"""Running one queued job: pipeline, ledger, delivery, and what the user is told (FR-9, FR-8, §8.2).

This is the seam :mod:`app.workers` was built for — a handler is ``(Job, LeaseKeeper) ->
JobOutcome`` — and it is deliberately thin. Every decision it makes is one the earlier parts
already made somewhere else:

* **What to fetch** comes from :func:`downloader.flow.download_link`, which owns the chain, the gate
  and the failure collection.
* **What to send** comes from :func:`telegram.delivery.deliver`, which owns albums, kinds and
  captions.
* **What to tell the user** comes from :mod:`app.catalog`, which owns the wording.

What is left here is bookkeeping, and three pieces of it are the reason this module exists:

* **The ledger** (§8.2.3). Every file that reached Telegram is recorded as a ``JobItem`` before the
  next one is attempted, and a file whose ledger row already says ``uploaded`` is never sent again.
  A job that loses its lease mid-delivery and is picked up again therefore resumes instead of
  re-delivering — which is the property E26/A-2 actually rest on, since the queue's own guard only
  covers whole jobs.
* **The quota** (FR-3.2). ``bytes_in`` is charged for what the network actually moved (D35), and the
  download reservation is released when nothing was delivered, so a failed job does not cost the
  user a download from their day.
* **The outcome** (spec 8.1). ``ok`` means *something reached the user*, which is exactly the
  difference between Delivered and Failed — a job whose every file failed is a Failed job, not a
  successful one with a long message.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyrogram import Client
from sqlalchemy import select

from app import catalog, paths, prefs, quota, tiers, workers
from app import db as db_module
from app.config import Settings
from app.models import Job, JobItem
from downloader import fetcher, flow, registry
from downloader.fetcher import FetchedFile
from telegram import delivery, links, media, status
from telegram.sessions import SessionError, get_pool

log = logging.getLogger(__name__)

#: What a job's own reserve is released to when nothing was delivered (FR-3.2).
DELIVERED = "uploaded"
FAILED = "failed"

#: The name a Telegram job's files are attributed to, for the log line and the status rows.
TELEGRAM_ENGINE = "telegram"

def build_handler(
    settings: Settings,
    *,
    sender_for: delivery.SenderFor,
    client: Client,
    chain_for: Callable[..., tuple[flow.Resolver, ...]] = registry.chain_for,
    delegate_for: Callable[..., flow.DelegatingFetch | None] = registry.delegate_for,
) -> workers.JobHandler:
    """The handler the worker pool runs for each claimed job.

    Injected rather than imported at the call site so the acceptance tests can drive a real pipeline
    against a recording client, and so a future per-host override is a parameter, not an edit.
    """

    async def handle(job: Job, keeper: workers.LeaseKeeper) -> workers.JobOutcome:
        user_id = job.user_id
        limits = tiers.get_limits(settings, job.tier)
        context = registry.ChainContext(
            preference=settings.download.video_preference,
            cookies_file=_cookies(user_id),
            options=await _adapter_options(settings, user_id=user_id),
        )
        already = await _ledger(job.id)

        # FR-7.1: one message per job, edited in place. It is the message /dl already posted, so the
        # renderer is told its id rather than allowed to create a second one.
        status_renderer = status.create_status_renderer(
            client,
            chat_id=job.status_chat_id or user_id,
            job_id=job.id,
            link=job.link,
            message_id=job.status_message_id,
            config=status.StatusConfig(
                render_interval=settings.ux.render_interval,
                anim_interval=settings.ux.anim_interval,
            ),
        )

        target = _telegram_target(job.link)
        if target is not None:
            # FR-4: a Telegram link is fetched with the *user's* own session, because the media
            # lives behind their account. Everything after this point is the same as any other job —
            # the ledger, the albums, the caption and the final message do not care where the bytes
            # came from, which is why the two paths meet again in ``LinkResult``.
            log.info("job %s: Telegram source", job.id)
            outcome = await _telegram_source(
                settings,
                job,
                target,
                paths.job_tmp_dir(job.id),
                keeper=keeper,
                status_renderer=status_renderer,
                limit_mb=limits.link_total_mb,
            )
        else:
            log.info("Starting download_link for job %s", job.id)
            outcome = await flow.download_link(
                job.link,
                paths.job_tmp_dir(job.id),
                limit_mb=limits.link_total_mb,
                chain=chain_for(job.link, context),
                delegate=delegate_for(context),
                should_stop=lambda: keeper.stop_requested,
                on_phase=_phase_hook(status_renderer),
                on_progress=status_renderer.progress,
                on_notice=status_renderer.notice,
            )
        log.info(
            "job %s: %d file(s), %d failure(s), cancelled=%s, refused=%s, empty=%s, reason=%s",
            job.id,
            len(outcome.files),
            len(outcome.failures),
            outcome.cancelled,
            outcome.refused is not None,
            outcome.empty_share,
            outcome.reason,
        )

        # FR-7.4's failure count is known now rather than during the download, because the pipeline
        # collects failures per file instead of streaming them.
        await status_renderer.failures(len(outcome.failures))

        # Ledger first, then send: a file sent but not recorded would be delivered twice on the next
        # attempt (FR-8.5), which is the worse of the two failures.
        fresh = [item for item in outcome.files if _identity(item) not in already]
        delivered: tuple[delivery.Delivered, ...] = ()
        if fresh:
            await status_renderer.start_uploading(len(fresh))
            delivered = await delivery.deliver(
                fresh,
                chat_id=job.status_chat_id or user_id,
                sender_for=sender_for,
                reply_to_message_id=job.status_message_id,
                caption_of=delivery.default_caption,
                album_size=settings.ux.album_size,
                on_upload=status_renderer.uploaded,
            )

        await _record(
            job.id,
            delivered=delivered,
            failures=outcome.failures,
            skipped=[item for item in outcome.files if item not in fresh],
        )
        await _charge(user_id, outcome)

        # FR-7.9: the final result *replaces* the progress message rather than following it. The
        # renderer owns that message, so the replacement happens there and this handler sends
        # nothing of its own.
        await status_renderer.finish(outcome)

        if outcome.cancelled:
            return workers.JobOutcome(ok=False, reason="cancelled", retry=False)
        if delivered:
            return workers.JobOutcome(ok=True)
        # A refusal or an empty share is a decision, not a hiccup: another attempt would reach the
        # same verdict (AC-3, E4). A file that failed is worth retrying, since hosts do recover.
        deterministic = outcome.refused is not None or outcome.empty_share
        return workers.JobOutcome(
            ok=False,
            reason=outcome.reason or catalog.NO_FILES_SENT,
            retry=not deterministic,
        )

    return handle


def _telegram_target(link: str) -> links.TgTarget | None:
    """The Telegram thing this job's link names, or ``None`` for every other link.

    Parsing decides the path, and it is the *same* parser ``/dt`` used to accept the link, so a job
    row is self-describing: a restart re-reads it and reaches the same conclusion without any stored
    "this one was Telegram" flag to fall out of step with the link itself.
    """
    if not _looks_like_telegram(link):
        return None
    try:
        return links.parse(link)
    except links.TgLinkError:
        # A t.me link the pipeline should have a go at (a channel's own page, say) rather than a job
        # that dies here: /dt refuses these, but a link that reached the queue by another route has
        # not been judged by anybody yet.
        return None


def _looks_like_telegram(link: str) -> bool:
    lowered = (link or "").lower()
    return any(host in lowered for host in links.HOSTS)


async def _telegram_source(
    settings: Settings,
    job: Job,
    target: links.TgTarget,
    staging: Path,
    *,
    keeper: workers.LeaseKeeper,
    status_renderer: status.StatusRenderer,
    limit_mb: int,
) -> flow.LinkResult:
    """Fetch a Telegram link with the user's own session, in the pipeline's vocabulary (FR-4).

    Failures are per *object*, which for Telegram means per message: a range that loses one message
    still delivers the rest, and the reason names that message so the user can see which one.
    """
    try:
        async with get_pool(settings).borrow(job.user_id) as session:
            fetched = await media.fetch(
                session,
                target,
                staging,
                limit_mb=limit_mb,
                should_stop=lambda: keeper.stop_requested,
                on_progress=_telegram_progress(status_renderer),
            )
    except SessionError as refused:
        return flow.LinkResult(reason=refused.reason, summary=refused.reason)

    failures = tuple(
        catalog.Failure(name=name, reason=reason) for name, reason in fetched.failures
    )
    if fetched.refused is not None:
        return flow.LinkResult(refused=fetched.refused, engine=TELEGRAM_ENGINE)
    if fetched.cancelled:
        return flow.LinkResult(files=fetched.files, failures=failures, cancelled=True)
    if fetched.error is not None:
        # A message that was reachable but carried nothing is E4's empty share — a verdict no retry
        # can change — while a chat we could not open is an ordinary failure worth another attempt.
        return flow.LinkResult(
            files=fetched.files,
            failures=failures,
            reason=fetched.error,
            summary=fetched.error,
            empty_share=fetched.empty,
        )

    return flow.LinkResult(
        files=fetched.files,
        failures=failures,
        engine=TELEGRAM_ENGINE,
        bytes_transferred=sum(file.bytes_written for file in fetched.files),
        bytes_written=sum(file.bytes_written for file in fetched.files),
        summary=catalog.DT_DONE.format(
            sent=len(fetched.files),
            total=len(fetched.files) + len(failures),
            scanned=fetched.scanned,
            chat=fetched.chat,
        ),
    )


def _telegram_progress(
    renderer: status.StatusRenderer,
) -> Callable[[int, int], Awaitable[None]]:
    """The pipeline's progress hook, from Telegram's per-file byte counts.

    A Telegram download reports no per-file index the way the HTTP fetcher does, so the rows show
    the bytes of the whole job rather than "file 3/20" — the same deliberate absence D43 accepted
    for an HLS stream, and for the same reason: a mis-stated counter is worse than a missing one.
    """

    async def report(done: int, total: int) -> None:
        await renderer.progress(
            flow.JobProgress(
                filename=TELEGRAM_ENGINE,
                transfer=fetcher.FetchProgress(
                    url=TELEGRAM_ENGINE,
                    role="video",
                    transfer_done=done,
                    transfer_total=total or None,
                    overall_done=done,
                    overall_total=total or None,
                ),
            )
        )

    return report


def _phase_hook(renderer: status.StatusRenderer) -> Callable[[str], Awaitable[None]]:
    """Translate the pipeline's phase names into the status message's (FR-7.3).

    The pipeline only owns the first two phases; ``uploading`` and ``done`` are the worker's, driven
    by the delivery steps the handler performs.
    """

    async def report(phase: str) -> None:
        mapped = status.Phase.DOWNLOADING if phase == "downloading" else status.Phase.RESOLVING
        await renderer.phase(mapped)

    return report


def _cookies(user_id: int) -> Path | None:
    """This user's cookie file, when they have set one (D8/FR-14)."""
    path = paths.cookie_path(user_id)
    return path if path.is_file() else None


async def _adapter_options(settings: Settings, *, user_id: int | None = None) -> dict[str, Any]:
    """Configuration an adapter or an engine needs, as plain values.

    Two scopes, both keyed by the name the strategy is known by: an adapter reads its own name
    (``reddit``), and gallery-dl reads ``gallery-dl``. The registry passes the matching slice
    through untouched (D39), so this is the one place that knows both what a consumer wants and
    where configuration keeps it — and the values are flat, because an adapter should not have to
    know that credentials live in a ``Settings`` model.

    ``gallery-dl``'s slice is already in *its* vocabulary (``extractor.reddit.client-id``) rather
    than in ours, because that string is a key in someone else's configuration file and translating
    it twice would only add a place to get it wrong.

    ``user_id`` is what makes the TeraBox slice per-user; every other scope is operator-level, so
    the parameter is optional and its absence simply omits that slice.
    """
    options: dict[str, Any] = {}

    reddit: dict[str, Any] = {}
    if settings.reddit_praw_client_id is not None:
        reddit["client_id"] = settings.reddit_praw_client_id.get_secret_value()
    if settings.reddit_praw_client_secret is not None:
        reddit["client_secret"] = settings.reddit_praw_client_secret.get_secret_value()
    if settings.reddit_praw_refresh_token is not None:
        reddit["refresh_token"] = settings.reddit_praw_refresh_token.get_secret_value()
    if reddit:
        options["reddit"] = reddit

    extractor: dict[str, str] = {}
    if settings.gdl_reddit_client_id is not None:
        extractor["extractor.reddit.client-id"] = (
            settings.gdl_reddit_client_id.get_secret_value()
        )
    if settings.gdl_reddit_refresh_token is not None:
        extractor["extractor.reddit.refresh-token"] = (
            settings.gdl_reddit_refresh_token.get_secret_value()
        )
    if extractor:
        options["gallery-dl"] = extractor

    if user_id is not None:
        terabox_cookie = await _terabox_cookie(settings, user_id)
        if terabox_cookie:
            options["terabox"] = {"cookies": terabox_cookie}

    return options


async def _terabox_cookie(settings: Settings, user_id: int) -> str | None:
    """Return this user's cookie first, followed by every configured fallback account.

    The user's own comes first because whichever account the cookie belongs to is the one TeraBox
    copies files into. Operator cookies follow as fallbacks so one blocked account does not stop
    the job; the adapter tries the pipe-delimited accounts in order.
    """
    async with db_module.session() as session:
        own = await prefs.terabox_cookie(session, user_id)
    accounts = ([own] if own else []) + settings.terabox_cookie_accounts()
    unique_accounts = list(dict.fromkeys(cookie.strip() for cookie in accounts if cookie.strip()))
    return "|".join(unique_accounts) or None


async def _note_phase(job_id: int, phase: str) -> None:
    """Record that the job started and what it is doing, which P4's status view reads (FR-7.3)."""
    async with db_module.session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        job.route = phase
        if job.started_at is None:
            job.started_at = datetime.now(UTC)


async def _ledger(job_id: int) -> set[str]:
    """What this job has already delivered, so a resumed attempt does not repeat it (FR-8.5)."""
    async with db_module.session() as session:
        result = await session.execute(
            select(JobItem.source_url).where(JobItem.job_id == job_id, JobItem.state == DELIVERED)
        )
        return {row.source_url for row in result if row.source_url}


def ledger_url(file: FetchedFile) -> str:
    """What identifies this file in the job ledger (``job_items.source_url``).

    The *resolved media* URL, never the pasted link, because one pasted album yields many files that
    all share the page URL — and the ledger's ``(job_id, source_url)`` constraint is what stops a
    resumed job uploading the same media twice (spec 8.2.3).

    A delegated download has no resolved URL at all (D38: a CLI fetched it), so it falls back to a
    ``file:`` URI of its own name, which is unique within the job and honest about being no URL.
    """
    return file.resolved_url or file.source_url or f"file:{file.path.name}"


def _identity(file: FetchedFile) -> str:
    """The ledger key for a file, in the same shape the ledger stores it."""
    return ledger_url(file)


async def _record(
    job_id: int,
    *,
    delivered: tuple[delivery.Delivered, ...],
    failures: tuple[catalog.Failure, ...],
    skipped: list[FetchedFile],
) -> None:
    """Write the ledger: what arrived, what failed, and what a previous attempt already sent."""
    async with db_module.session() as session:
        existing = await session.execute(
            select(JobItem.source_url)
            .where(JobItem.job_id == job_id)
        )
        seen = {row[0] for row in existing}

        next_index = (
            await session.execute(
                select(JobItem.idx)
                .where(JobItem.job_id == job_id)
                .order_by(JobItem.idx.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or 0

        def _row(**kwargs: Any) -> JobItem:  # type: ignore[no-untyped-def]
            """Build a JobItem, skipping rows whose ledger key already exists for this job."""
            if kwargs["source_url"] in seen:
                return None  # type: ignore[return-value]
            seen.add(kwargs["source_url"])
            return JobItem(**kwargs)  # type: ignore[call-arg]

        for item in delivered:
            session.add(
                _row(
                    job_id=job_id,
                    idx=next_index,
                    source_url=ledger_url(item.file),
                    filename=item.file.path.name,
                    media_kind=item.file.media_kind,
                    bytes_total=item.file.bytes_written,
                    bytes_done=item.file.bytes_written,
                    state=DELIVERED,
                    uploaded_message_id=item.message_ids[0] if item.message_ids else None,
                )
            )
            next_index += 1

        for failure in failures:
            session.add(
                _row(
                    job_id=job_id,
                    idx=next_index,
                    # A failure has no resolved URL of its own: it never got that far.
                    source_url=f"failed:{failure.name}",
                    filename=failure.name,
                    state=FAILED,
                    reason=failure.reason,
                )
            )
            next_index += 1

        # A file a previous attempt delivered is recorded too, so the ledger stays the full record.
        for file in skipped:
            session.add(
                _row(
                    job_id=job_id,
                    idx=next_index,
                    source_url=ledger_url(file),
                    filename=file.path.name,
                    media_kind=file.media_kind,
                    bytes_total=file.bytes_written,
                    state=DELIVERED,
                )
            )
            next_index += 1


async def _charge(user_id: int, outcome: flow.LinkResult) -> None:
    """Bandwidth and the download reservation (FR-3.2, D35)."""
    async with db_module.session() as session:
        if outcome.bytes_transferred:
            await quota.record_bytes(session, user_id, inbound=outcome.bytes_transferred)
        if not outcome.files:
            # Nothing was delivered, so the day's download is handed back rather than spent.
            await quota.release_download(session, user_id)
