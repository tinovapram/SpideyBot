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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app import catalog, paths, quota, tiers, workers
from app import db as db_module
from app.config import Settings
from app.models import Job, JobItem
from downloader import flow, registry
from downloader.fetcher import FetchedFile
from telegram import delivery

log = logging.getLogger(__name__)

#: What a job's own reserve is released to when nothing was delivered (FR-3.2).
DELIVERED = "uploaded"
FAILED = "failed"


@dataclass(frozen=True)
class JobDialogue:
    """The messages a finished job produces, before anything is sent.

    Returning the text rather than sending it keeps the wording decisions testable on their own, and
    keeps this module from being the second place that knows catalog's format strings.
    """

    body: str
    delivered: int
    failed: int


def final_message(outcome: flow.LinkResult, *, total: int) -> JobDialogue:
    """FR-7.9's replacement message, in the shapes the spec fixes.

    The failure block is appended rather than sent separately (FR-9.4), so a job with 17 successes
    and 3 failures stays one message.
    """
    delivered = len(outcome.files)
    failed = len(outcome.failures)
    block = catalog.failure_block(list(outcome.failures))

    if outcome.refused is not None:
        body = catalog.limit_exceeded_message(
            actual_mb=outcome.refused.actual_mb, limit_mb=outcome.refused.limit_mb
        )
        return JobDialogue(body=body, delivered=0, failed=0)

    if outcome.cancelled:
        return JobDialogue(body=catalog.DOWNLOAD_CANCELLED, delivered=0, failed=failed)

    if outcome.empty_share:
        return JobDialogue(body=catalog.EMPTY_SHARE, delivered=0, failed=0)

    if delivered == 0:
        reason = outcome.reason or catalog.NO_FILES_SENT
        body = catalog.ERROR_OCCURRED.format(error=reason)
        return JobDialogue(body=f"{block}\n{body}" if block else body, delivered=0, failed=failed)

    body = catalog.DONE_WITH_FILES.format(
        sent=delivered, total=max(total, delivered), size=_mb(outcome.bytes_written)
    )
    return JobDialogue(
        body=f"{body}\n{block}" if block else body, delivered=delivered, failed=failed
    )


def _mb(count: int) -> str:
    """Whole megabytes for the done message, which shows ``{size} MB`` (spec §7.6)."""
    return f"{count / (1024 * 1024):.1f}".rstrip("0").rstrip(".")


def build_handler(
    settings: Settings,
    *,
    sender_for: delivery.SenderFor,
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
        )
        already = await _ledger(job.id)

        outcome = await flow.download_link(
            job.link,
            paths.job_tmp_dir(job.id),
            limit_mb=limits.link_total_mb,
            chain=chain_for(job.link, context),
            delegate=delegate_for(context),
            should_stop=lambda: keeper.stop_requested,
            on_phase=lambda phase: _note_phase(job.id, phase),
        )

        # Ledger first, then send: a file sent but not recorded would be delivered twice on the next
        # attempt (FR-8.5), which is the worse of the two failures.
        fresh = [item for item in outcome.files if _identity(item) not in already]
        delivered: tuple[delivery.Delivered, ...] = ()
        if fresh:
            delivered = await delivery.deliver(
                fresh,
                chat_id=job.status_chat_id or user_id,
                sender_for=sender_for,
                reply_to_message_id=job.status_message_id,
                caption_of=delivery.default_caption,
                album_size=settings.ux.album_size,
            )

        await _record(
            job.id,
            delivered=delivered,
            failures=outcome.failures,
            skipped=[item for item in outcome.files if item not in fresh],
        )
        await _charge(user_id, outcome)
        await _notify(
            sender_for,
            job,
            final_message(outcome, total=len(outcome.files) + len(outcome.failures)),
        )

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


def _cookies(user_id: int) -> Path | None:
    """This user's cookie file, when they have set one (D8/FR-14)."""
    path = paths.cookie_path(user_id)
    return path if path.is_file() else None


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
            select(JobItem.idx)
            .where(JobItem.job_id == job_id)
            .order_by(JobItem.idx.desc())
            .limit(1)
        )
        next_index = (existing.scalar_one_or_none() or 0) + 1

        for item in delivered:
            session.add(
                JobItem(
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
                JobItem(
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
                JobItem(
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


async def _notify(sender_for: delivery.SenderFor, job: Job, dialogue: JobDialogue) -> None:
    """The one message a job ends with (FR-7.9)."""
    chat_id = job.status_chat_id or job.user_id
    sender = await delivery.resolve_sender(sender_for, chat_id)
    try:
        await sender.send_message(
            chat_id=chat_id,
            text=dialogue.body,
            reply_to_message_id=job.status_message_id,
        )
    except Exception:  # noqa: BLE001 - a user who blocked the bot must not fail their job twice
        log.warning("could not report the result of job %s", job.id, exc_info=True)
