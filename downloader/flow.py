"""One link, start to finish: resolve, gate, fetch, and report (P3, D6, D25, D31).

This is the part that decides *what happens*, as opposed to how any single step works. It owns the
fallback chain (native → gallery-dl → cyberdrop-dl, D6), the size gate's verdict for the whole link
(AC-3), the per-file download loop with FR-9's failure collection, and the vocabulary the status
layer and the final message are built from.

Everything external is injected — the resolver chain, the stop signal, the three notification hooks
and the HTTP session — so the whole pipeline can be driven in a test without network, database or
Telegram, and so the worker handler that eventually calls it stays a thin adapter.

Three rules here are decisions rather than plumbing:

* **A link produces an outcome, not an exception.** A refusal (AC-3), an empty share (E4) and a
  total failure (E2) are all ordinary results of asking a link for its files, and all three have
  their own copy. Raising for them would push the difference into whichever caller happened to
  catch it, which is how a user ends up seeing "an error occurred" for a folder-only share.
* **A file that fails does not stop the others** (FR-9.1, E5). Failures are collected with the name,
  the reason and the source URL so the final message can name them (AC-15), and the job is still a
  success if anything arrived.
* **The chain moves on for either kind of nothing.** Both "found no files" and "the extractor
  failed" mean this resolver cannot help, so the next one is tried (E2). Only what the resolvers
  *said* is kept, so the user's message can distinguish an empty share from a broken link.

Resolution happens inside the worker rather than at submission: it needs the network, and the
submission path has to answer the user immediately (FR-10.2). The cost is that E4's "no job queued"
is really "a job that stopped before transferring anything", which the job lifecycle already
expresses as Failed with a reason (spec 8.1) — no transfer, no delivery, and the empty share's own
message.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from curl_cffi import requests

from app import catalog
from downloader import fetcher, gate
from downloader.contracts import (
    MediaKind,
    Resolution,
    ResolvedFile,
    ResolveError,
    safe_filename,
    unique_name,
)
from downloader.fetcher import (
    FetchedFile,
    FetchError,
    FetchProgress,
    TransferCancelled,
)

log = logging.getLogger(__name__)

#: FR-7.3's phases. Flow owns the first two; delivery adds Uploading and Done.
Phase = Literal["resolving", "downloading"]

#: A user-facing notice while the job runs — today, the E2 fallback line.
Notification = Callable[[str], Awaitable[None] | None]
PhaseHook = Callable[[Phase], Awaitable[None] | None]
StopCheck = Callable[[], bool]


@dataclass(frozen=True)
class Resolver:
    """One attempt strategy in the chain, named so the user can be told which one is running.

    The name is shown in E2's notice, so it is user-facing copy and lives with the provider rather
    than being derived from a module path.
    """

    name: str
    attempt: Callable[[str], Awaitable[Resolution]]


@dataclass(frozen=True)
class DelegatingFetch:
    """A fetch strategy that performs the transfer itself, handing back finished files (D38).

    The last resort is a *program* that downloads, so it cannot join the resolver chain: it produces
    files rather than transfers, which is why the pipeline has two strategies instead of one. The
    cost is the pre-transfer size gate — a CLI cannot say what it is about to fetch, so the cap is
    enforced by watching the download instead (D31's fallback), and that cost is why it runs last.
    """

    name: str
    run: Callable[..., Awaitable[tuple[FetchedFile, ...]]]


@dataclass(frozen=True)
class JobProgress:
    """The fetcher's numbers, plus where this file sits in the job (FR-7.4, FR-7.5).

    The fetcher knows nothing about jobs on purpose, so the counting happens here rather than inside
    it. ``index`` and ``total`` are absent for a delegated run, which reports aggregate bytes only:
    reporting "file 1/1" for a tool that is fetching twenty files would be a worse lie than
    reporting none.
    """

    filename: str
    transfer: FetchProgress
    index: int | None = None
    total: int | None = None

    @property
    def file_counter(self) -> str:
        """``3/20``, the shape the status rows ask for, or empty when there is no per-file count."""
        if self.index is None or self.total is None:
            return ""
        return f"{self.index}/{self.total}"


ProgressHook = Callable[[JobProgress], Awaitable[None] | None]


@dataclass(frozen=True)
class LinkResult:
    """What one link produced: files on disk, failures, and why when nothing came of it."""

    files: tuple[FetchedFile, ...] = ()
    failures: tuple[catalog.Failure, ...] = ()
    #: Which resolver supplied the files, for the log and for `/status` diagnostics.
    engine: str | None = None
    #: AC-3's refusal, when the link was judged too large *before* any transfer. The decision
    #: itself is returned rather than rendered, so the gate's numbers and the message cannot drift.
    refused: gate.SizeCheck | None = None
    #: E4: no resolver found any media, and none of them reported a failure either.
    empty_share: bool = False
    #: E2: every resolver failed; this is the most specific reason any of them gave.
    reason: str | None = None
    #: FR-11: the user stopped the job. Whatever was fetched is for discarding, not delivery.
    cancelled: bool = False
    #: Bytes pulled off the network, including partials that were discarded (D35).
    bytes_transferred: int = 0
    #: Bytes inside the files that are actually being handed to delivery.
    bytes_written: int = 0
    #: A sentence the *source* wants shown as the job's result, when it has one.
    #:
    #: The pipeline's shapes (refusal, empty share, failure, done-with-files) are built from the
    #: numbers, which works when a source's own vocabulary is the same as everyone else's. Telegram
    #: has more to say — FR-4.5 wants the source chat's title and how many messages were *scanned*,
    #: and FR-4.6's refusals are sentences written where the parsing happened — so the source
    #: supplies one line and :func:`app.messages.final_message` shows it instead of inventing a
    #: second phrasing for the same fact.
    summary: str | None = None

    @property
    def delivered_anything(self) -> bool:
        return bool(self.files)

    @property
    def stopped_early(self) -> bool:
        """True when nothing should be sent and the job is not a success: AC-3, E2, E4 or cancel."""
        return self.refused is not None or self.empty_share or bool(self.reason) or self.cancelled

    @property
    def media_kind(self) -> MediaKind:
        """The kind of the first file, for a caller that needs one classification for the job."""
        return self.files[0].media_kind if self.files else "document"


async def _notify(hook: Notification | None, message: str) -> None:
    """Call a hook that may be sync or async, and may be absent."""
    if hook is None:
        return
    result = hook(message)
    if isinstance(result, Awaitable):
        await result


async def _phase(hook: PhaseHook | None, phase: Phase) -> None:
    if hook is None:
        return
    result = hook(phase)
    if isinstance(result, Awaitable):
        await result


async def _progress(hook: ProgressHook | None, update: JobProgress) -> None:
    if hook is None:
        return
    result = hook(update)
    if isinstance(result, Awaitable):
        await result


async def resolve_through_chain(
    url: str,
    chain: Sequence[Resolver],
    *,
    on_notice: Notification | None = None,
) -> Resolution:
    """Try each resolver in turn and return the first that finds files.

    Raises :class:`ResolveError` when every attempt came up empty, carrying the last *failure*
    reason when there was one — a share with no media says so instead of inventing an error, which
    is the E4/E2 distinction the user's message depends on.
    """
    if not chain:
        raise ResolveError("no resolver is configured for that link")

    failure: ResolveError | None = None
    for position, resolver in enumerate(chain):
        if position:
            # E2's visible phase change: the user is told why the job is starting over.
            notice = catalog.fallback_notice(resolver.name)
            log.info("resolving %s: falling back to %s", url, resolver.name)
            await _notify(on_notice, notice)

        try:
            resolution = await resolver.attempt(url)
        except ResolveError as error:
            log.info("resolver %s could not handle %s: %s", resolver.name, url, error)
            if not error.empty:
                failure = error
            continue

        if resolution.files:
            return resolution
        log.info("resolver %s found nothing at %s", resolver.name, url)

    if failure is not None:
        raise failure
    raise ResolveError("no downloadable media found at that link", empty=True)


async def download_link(
    url: str,
    staging: Path,
    *,
    limit_mb: int,
    chain: Sequence[Resolver],
    delegate: DelegatingFetch | None = None,
    should_stop: StopCheck | None = None,
    on_phase: PhaseHook | None = None,
    on_progress: ProgressHook | None = None,
    on_notice: Notification | None = None,
    transfer_timeout: float = fetcher.DEFAULT_TIMEOUT,
    impersonate: str | None = fetcher.DEFAULT_IMPERSONATE,
    session: Any | None = None,
) -> LinkResult:
    """Turn one link into the files the user will receive.

    ``staging`` is the job's own directory (``paths.job_tmp_dir``); it is created if absent and is
    never cleaned up here, because the caller owns its lifetime — the files have to outlive this
    call to be uploaded.

    ``chain`` is supplied by the caller (:mod:`downloader.registry`) and carries its own settings —
    cookies, the D28 preference, a TeraBox account pool. Keeping them in the chain is what lets an
    adapter arrive without this signature changing.

    ``delegate`` is the last resort (D38): tried once the chain has failed, and only then, because
    it cannot be measured before it runs.
    """
    staging.mkdir(parents=True, exist_ok=True)
    stop = should_stop or (lambda: False)

    await _phase(on_phase, "resolving")
    try:
        resolution = await resolve_through_chain(url, chain, on_notice=on_notice)
    except ResolveError as error:
        # E2's last resort. Its cap is the tier's, unmeasured on purpose: nobody can report the size
        # of what a CLI is about to fetch, so the limit is enforced by watching the run (D31/D38).
        if delegate is not None:
            return await _delegate(
                delegate,
                url,
                staging,
                limit_mb=limit_mb,
                should_stop=should_stop,
                on_progress=on_progress,
                on_notice=on_notice,
                on_phase=on_phase,
            )
        if error.empty:
            return LinkResult(empty_share=True, reason=str(error))
        return LinkResult(reason=str(error))

    # AC-3: the whole link is judged before a single byte moves, on what resolution reported.
    verdict = gate.check_size(resolution.total_bytes, limit_mb=limit_mb)
    if not verdict.allowed:
        log.info(
            "refusing %s before transfer: %s of %s MB", url, verdict.actual_mb, verdict.limit_mb
        )
        return LinkResult(refused=verdict, engine=resolution.engine)

    await _phase(on_phase, "downloading")
    # D31's flag: an unknown size is enforced during the transfer instead of being refused here.
    transfer_cap = verdict.limit_bytes if verdict.enforce_during_transfer else None

    files: list[FetchedFile] = []
    failures: list[catalog.Failure] = []
    taken: set[str] = set()
    transferred = 0
    total = len(resolution.files)
    owns_session = session is None
    client = session or requests.AsyncSession()

    try:
        for index, item in enumerate(resolution.files, start=1):
            if stop():
                log.info("job for %s cancelled before file %d/%d", url, index, total)
                return LinkResult(
                    files=tuple(files),
                    failures=tuple(failures),
                    engine=resolution.engine,
                    cancelled=True,
                    bytes_transferred=transferred,
                    bytes_written=sum(f.bytes_written for f in files),
                )

            destination = staging / unique_name(safe_filename(item.filename), taken)
            taken.add(destination.name)
            try:
                fetched = await fetcher.fetch(
                    item,
                    destination,
                    limit_bytes=transfer_cap,
                    progress=_file_reporter(on_progress, item, index, total),
                    session=client,
                    timeout=transfer_timeout,
                    impersonate=impersonate,
                    should_stop=should_stop,
                )
            except TransferCancelled:
                log.info("job for %s cancelled during file %d/%d", url, index, total)
                taken.discard(destination.name)
                return LinkResult(
                    files=tuple(files),
                    failures=tuple(failures),
                    engine=resolution.engine,
                    cancelled=True,
                    bytes_transferred=transferred,
                    bytes_written=sum(f.bytes_written for f in files),
                )
            except FetchError as error:
                # FR-9.1/E5: one bad file must not cost the user the other nineteen.
                log.info("file %s of %s failed: %s", destination.name, url, error)
                # A cap breach knows how much it had pulled; a dropped connection does not. Counted
                # as transferred either way, because that traffic really happened (D35).
                transferred += getattr(error, "written_bytes", 0)
                taken.discard(destination.name)
                failures.append(
                    catalog.Failure(name=item.filename, reason=str(error), url=item.source_url)
                )
                continue

            files.append(fetched)
            transferred += fetched.bytes_written

        return LinkResult(
            files=tuple(files),
            failures=tuple(failures),
            engine=resolution.engine,
            bytes_transferred=transferred,
            bytes_written=sum(f.bytes_written for f in files),
        )
    finally:
        if owns_session:
            await client.close()


def _file_reporter(
    hook: ProgressHook | None,
    item: ResolvedFile,
    index: int,
    total: int,
) -> fetcher.ProgressHook | None:
    """Wrap the caller's progress hook so each update knows which file it belongs to (FR-7.5)."""
    if hook is None:
        return None

    async def report(update: FetchProgress) -> None:
        await _progress(
            hook,
            JobProgress(index=index, total=total, filename=item.filename, transfer=update),
        )

    return report


async def _delegate(
    delegate: DelegatingFetch,
    url: str,
    staging: Path,
    *,
    limit_mb: int,
    should_stop: StopCheck | None,
    on_progress: ProgressHook | None,
    on_notice: Notification | None,
    on_phase: PhaseHook | None,
) -> LinkResult:
    """Run the last resort, and report what it produced or why it produced nothing."""
    await _notify(on_notice, catalog.fallback_notice(delegate.name))
    await _phase(on_phase, "downloading")

    # The cap without a measurement: exactly D31's case, arrived at from the other direction.
    verdict = gate.check_size(None, limit_mb=limit_mb)

    async def report(update: FetchProgress) -> None:
        await _progress(
            on_progress,
            JobProgress(filename=url, transfer=update),
        )

    try:
        files = await delegate.run(
            url,
            staging,
            limit_bytes=verdict.limit_bytes,
            progress=report if on_progress is not None else None,
            should_stop=should_stop,
        )
    except TransferCancelled:
        return LinkResult(cancelled=True)
    except FetchError as failure:
        # FetchError, not ResolveError: this strategy failed while *fetching*, and its message is
        # either the tool's own recorded reason or a plain statement that nothing arrived.
        return LinkResult(reason=str(failure))

    written = sum(item.bytes_written for item in files)
    return LinkResult(
        files=tuple(files),
        engine=delegate.name,
        bytes_written=written,
        bytes_transferred=written,
    )
