"""The one status message a job owns, and the final result that replaces it (FR-7, E7, E8).

FR-7.1 is the constraint everything else here follows from: **one** message per job, edited in
place. So this module owns a message *id* rather than a stream of messages, and that id is the job's
own stored ``status_message_id`` — which is what lets a worker that picks a job up after a restart
keep editing the message the user is already watching instead of starting a second one.

Four user-visible rules live here rather than in the caller:

* **Coalescing** (FR-7.7). A transfer reports every 256 KiB, so a 500 MB file would be hundreds of
  edits and a FloodWait. Content edits are throttled to ``render_interval`` and the animation
  (FR-7.6) to ``anim_interval``, which bounds the edit rate to the faster of the two.
* **The final result is never dropped** (E8). FR-7.9's replacement bypasses both intervals.
* **Deletion is survivable** (E7). A ``MessageIdInvalid`` on edit recreates the message *once*; a
  second deletion is logged and given up on, because a user who keeps deleting it is not asking for
  a third copy.
* **A flood wait is waited out** (E8), not raised into the job: whether a download succeeds must not
  depend on Telegram's mood.

Nothing here decides *what* a job is doing — the phases come from :mod:`downloader.flow` through
``on_phase``/``on_progress``, and the wording comes from :mod:`app.catalog` (D23).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pyrogram.errors import FloodWait, MessageIdInvalid, MessageNotModified

from app import catalog
from app.messages import JobDialogue, final_message
from downloader.flow import JobProgress, LinkResult

log = logging.getLogger(__name__)

#: FR-7.6's liveness animation. Deliberately cycling through "nothing" first, so a stalled transfer
#: still shows a change rather than only gaining dots.
ELLIPSIS_FRAMES = ("", ".", "..", "...")

#: Width of the textual bar in FR-7.4's transfer row.
BAR_WIDTH = 10

#: Long enough for a filename to be recognisable, short enough not to wrap the message three times.
NAME_LIMIT = 48
LINK_LIMIT = 60


class Phase(StrEnum):
    """FR-7.3's four phases, as the status message names them.

    ``DONE`` never reaches the user as a *header*: FR-7.9 replaces the message with the result, and
    that result's own wording is the done line (``✅ SpideyBot: Done! Sent 3/3 files``). The label
    exists so the phase is still a value the renderer and its callers agree on.
    """

    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    DONE = "done"


_LABELS: dict[Phase, str] = {
    Phase.RESOLVING: catalog.PHASE_RESOLVING,
    Phase.DOWNLOADING: catalog.PHASE_DOWNLOADING,
    Phase.UPLOADING: catalog.PHASE_UPLOADING,
    Phase.DONE: catalog.PHASE_DONE,
}

#: The phases that are still working, and therefore animate (FR-7.6).
_LIVE = (Phase.RESOLVING, Phase.DOWNLOADING, Phase.UPLOADING)


class StatusClient(Protocol):
    """The only two calls a status message needs.

    Narrow on purpose: a fake is then four lines, and a third call added to this module is a change
    the fake complains about rather than hides.
    """

    async def send_message(self, **kwargs: Any) -> Any: ...

    async def edit_message_text(self, **kwargs: Any) -> Any: ...


#: The two throttles, defaulted to the committed ``[ux]`` values.
@dataclass(frozen=True)
class StatusConfig:
    """The throttles for content edits and for the animation (FR-7.6, FR-7.7)."""

    render_interval: float = 3.0
    anim_interval: float = 1.2


@dataclass
class StatusState:
    """What the message should say right now. One instance per job."""

    chat_id: int
    job_id: int
    link: str = ""
    #: The message already posted for this job (``job.status_message_id``). ``None`` means this
    #: renderer has to create it — a job queued without one, or E7's recreation.
    message_id: int | None = None
    phase: Phase = Phase.RESOLVING
    current_file: str = ""
    transfer_done: int = 0
    transfer_total: int | None = None
    files_total: int = 0
    files_done: int = 0
    upload_index: int = 0
    files_uploaded: int = 0
    files_sent: int = 0
    files_failed: int = 0
    notice: str = ""
    anim_frame: int = 0
    last_content: float = 0.0
    last_anim: float = 0.0
    #: E7 allows exactly one recreation; this is the flag that makes the second deletion a no-op.
    recreated: bool = False
    finished: bool = False
    last_text: str = ""


class StatusRenderer:
    """Renders, throttles, and edits one job's status message."""

    def __init__(
        self,
        client: StatusClient,
        state: StatusState,
        config: StatusConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.state = state
        self.config = config or StatusConfig()
        # Injected so a test can drive the two intervals without sleeping, and so E8's flood-wait
        # path is a test rather than a hope.
        self._clock = clock
        self._sleep = sleep

    # ── the message body (FR-7.2) ────────────────────────────────────────

    def render(self) -> str:
        """The whole message: header, rows, footer."""
        state = self.state
        lines = [
            catalog.JOB_HEADER.format(
                phase=_LABELS[state.phase],
                ellipsis=self._ellipsis() if state.phase in _LIVE else "",
            ),
            catalog.JOB_ID_LINE.format(id=state.job_id),
        ]
        if state.link:
            lines.append(catalog.JOB_LINK_LINE.format(link=_shorten(state.link, LINK_LIMIT)))

        rows = self._rows()
        if rows:
            lines.append("")
            lines.extend(rows)

        lines.append("")
        lines.append(catalog.JOB_FOOTER.format(id=state.job_id))
        return "\n".join(lines)

    def _rows(self) -> list[str]:
        """FR-7.4's live counters, in the order the job produces them.

        The download counter and the upload counters are mutually exclusive, because they describe
        different work: showing ``Downloaded: 0/25`` while the albums are going out would report a
        number that is finished and make the upload look stalled.
        """
        state = self.state
        rows: list[str] = []

        if state.notice:
            rows.append(catalog.JOB_NOTICE.format(notice=state.notice))

        if state.phase in (Phase.DOWNLOADING, Phase.UPLOADING) and state.current_file:
            rows.append(self._transfer_row())

        if state.files_total and state.phase != Phase.UPLOADING:
            rows.append(
                catalog.JOB_FILES_DOWNLOADED.format(done=state.files_done, total=state.files_total)
            )

        if state.phase == Phase.UPLOADING and state.upload_index:
            rows.append(
                catalog.JOB_UPLOADING_FILE.format(
                    index=state.upload_index, total=state.files_total
                )
            )

        if state.files_uploaded:
            rows.append(
                catalog.JOB_FILES_UPLOADED.format(
                    done=state.files_uploaded, total=state.files_total
                )
            )

        if state.files_sent:
            rows.append(
                catalog.JOB_FILES_SENT.format(done=state.files_sent, total=state.files_total)
            )

        if state.files_failed:
            rows.append(catalog.JOB_FILES_FAILED.format(count=state.files_failed))

        return rows

    def _transfer_row(self) -> str:
        """The current transfer: a bar when a total is known, bare bytes when it is not."""
        name = _shorten(self.state.current_file, NAME_LIMIT)
        total = self.state.transfer_total
        done = self.state.transfer_done
        if not total:
            # An unknown total is normal — an HLS stream and a chunked response both have none — and
            # inventing a percentage for it would be a precise-looking lie (D30).
            return catalog.JOB_TRANSFER_UNSIZED.format(name=name, done=_bytes(done))

        filled = min(BAR_WIDTH, int(done / total * BAR_WIDTH))
        return catalog.JOB_TRANSFER.format(
            name=name,
            bar="▰" * filled + "▱" * (BAR_WIDTH - filled),
            percent=f"{done / total * 100:.1f}",
            done=_bytes(done),
            total=_bytes(total),
        )

    def _ellipsis(self) -> str:
        return ELLIPSIS_FRAMES[self.state.anim_frame % len(ELLIPSIS_FRAMES)]

    # ── what the pipeline and the worker tell it ─────────────────────────

    async def phase(self, phase: Phase) -> None:
        """A phase change (FR-7.3). Rare and meaningful, so it is not left to the throttle."""
        self.state.phase = phase
        self.state.anim_frame = 0
        await self._tick(content=True, force=True)

    async def progress(self, update: JobProgress) -> None:
        """One fetcher progress report (FR-7.4).

        The *file-level* numbers are the ones FR-7.4 asks for, so ``overall_*`` is used rather than
        the per-stream ``transfer_*``: in a merged video the second stream continues the first's
        count instead of restarting it (D30).
        """
        state = self.state
        state.current_file = update.filename
        state.transfer_done = update.transfer.overall_done
        state.transfer_total = update.transfer.overall_total
        if update.total:
            state.files_total = update.total
        # ``index`` is the file in flight, so the count of finished files is everything before it.
        state.files_done = max(update.index - 1, 0) if update.index else 0
        await self._tick(content=True)

    async def notice(self, message: str) -> None:
        """E2's fallback notice, kept in the message so the reason survives (FR-7.2)."""
        self.state.notice = message
        await self._tick(content=True, force=True)

    async def failures(self, count: int) -> None:
        """FR-7.4's failure count.

        Known only once the pipeline returns, because a per-file failure is collected by
        :func:`downloader.flow.download_link` rather than streamed as it happens — so this is called
        when the download phase ends, not while it runs.
        """
        self.state.files_failed = count
        await self._tick(content=True, force=True)

    async def start_uploading(self, total: int) -> None:
        """Delivery is beginning (FR-7.3), with the number of files the albums will carry.

        ``upload_index`` starts at 1 so FR-7.5's per-file row is present from the first edit rather
        than only after an album has landed — the first file *is* in flight at this moment.
        """
        state = self.state
        state.phase = Phase.UPLOADING
        state.files_total = total
        state.upload_index = 1 if total else 0
        state.current_file = ""
        state.transfer_done = 0
        state.transfer_total = None
        state.anim_frame = 0
        await self._tick(content=True, force=True)

    async def uploaded(self, done: int, total: int) -> None:
        """Per-file upload context, as each album lands (FR-7.5).

        ``done`` counts files that reached Telegram, so the row reads as the spec's own example —
        ``Uploading (file 7/25)`` — with the file in flight implied by what has already arrived.
        """
        state = self.state
        state.files_uploaded = done
        state.files_sent = done
        state.upload_index = min(done + 1, total) if done < total else total
        state.files_total = total
        await self._tick(content=True)

    async def finish(self, outcome: LinkResult) -> JobDialogue:
        """FR-7.9: the final result **replaces** this message. Returns what was rendered.

        Returned rather than only sent so the wording stays testable on its own, and so a caller can
        tell a delivery that happened from one that was only composed.
        """
        dialogue = final_message(outcome, total=len(outcome.files) + len(outcome.failures))
        self.state.finished = True
        self.state.phase = Phase.DONE
        # E8: written directly rather than through the throttles, because a dropped final result is
        # the one failure a retry cannot fix — the user would be left watching a message that never
        # finished. FR-7.9: this *replaces* the progress message rather than following it.
        await self._write(dialogue.body)
        return dialogue

    # ── the throttled write path (FR-7.7, E7, E8) ────────────────────────

    async def _tick(self, *, content: bool, force: bool = False) -> None:
        """Render if either interval allows it (FR-7.7)."""
        state = self.state
        if state.finished and not force:
            return

        now = self._clock()
        content_due = force or (content and now - state.last_content >= self.config.render_interval)
        anim_due = state.phase in _LIVE and now - state.last_anim >= self.config.anim_interval
        if not (content_due or anim_due):
            return

        if content_due:
            state.last_content = now
        if anim_due:
            state.anim_frame += 1
            state.last_anim = now
        await self._write(self.render())

    async def _write(self, text: str, *, retried: bool = False) -> None:
        """Send the first time, edit afterwards, and survive E7 and E8."""
        state = self.state
        try:
            if state.message_id is None:
                message = await self.client.send_message(chat_id=state.chat_id, text=text)
                state.message_id = getattr(message, "id", None)
                log.debug("job %s: status message %s created", state.job_id, state.message_id)
            else:
                await self.client.edit_message_text(
                    chat_id=state.chat_id, message_id=state.message_id, text=text
                )
            state.last_text = text
        except MessageNotModified:
            # Only the ellipsis came back round to the same frame; nothing to say about it.
            state.last_text = text
        except MessageIdInvalid:
            if state.recreated:
                # E7 says recreate it and continue. Recreating a third time would make the bot the
                # thing that is spamming the chat.
                log.warning(
                    "job %s: status message deleted twice; not recreating again", state.job_id
                )
                return
            log.info("job %s: status message deleted; recreating once (E7)", state.job_id)
            state.recreated = True
            state.message_id = None
            await self._write(text)
        except FloodWait as error:
            seconds = float(getattr(error, "value", 0) or 0)
            if retried:
                log.warning("job %s: still rate-limited; dropping this update (E8)", state.job_id)
                return
            log.info("job %s: rate-limited for %.0fs (E8)", state.job_id, seconds)
            await self._sleep(seconds)
            await self._write(text, retried=True)
        except Exception:  # noqa: BLE001 - reporting must never fail the download it reports on
            log.warning("job %s: could not update the status message", state.job_id, exc_info=True)


def create_status_renderer(
    client: StatusClient,
    *,
    chat_id: int,
    job_id: int,
    link: str = "",
    message_id: int | None = None,
    config: StatusConfig | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> StatusRenderer:
    """The renderer for one job, editing the message ``/dl`` already posted (FR-7.1)."""
    return StatusRenderer(
        client,
        StatusState(chat_id=chat_id, job_id=job_id, link=link, message_id=message_id),
        config,
        clock=clock,
        sleep=sleep,
    )


def _shorten(text: str, limit: int) -> str:
    """Truncate the tail, so the beginning (the host, the filename) survives."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bytes(count: int) -> str:
    """One decimal, because FR-7.4's row is a glance and more digits are noise at that size."""
    value = float(count)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
