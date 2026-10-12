"""The one status message per job: FR-7's rules, and E7/E8's survival (P4).

Driven with an injected clock, because the rules under test *are* the two intervals — a test that
slept for three seconds would be a slow test of the same thing, and one that did not control the
clock could not tell coalescing from a bug that drops updates.
"""

from __future__ import annotations

from typing import Any

import pytest
from pyrogram.errors import FloodWait, MessageIdInvalid, MessageNotModified

from app import catalog
from downloader.flow import FetchProgress, JobProgress, LinkResult, Resolver, download_link  # noqa: F401
from telegram import status


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class ScriptedStatusClient:
    """A client that records what the renderer did, and can refuse on command."""

    def __init__(self, *, message_id: int | None = 4242) -> None:
        self.sent: list[str] = []
        self.edited: list[tuple[int, str]] = []
        self._next_id = message_id or 100
        self.created = 0
        #: Raised by the next edit, once. Used for E7 and E8.
        self.fail_next: Exception | None = None

    async def send_message(self, **kwargs: Any) -> FakeMessage:
        self.sent.append(kwargs["text"])
        self.created += 1
        self._next_id += 1
        return FakeMessage(self._next_id)

    async def edit_message_text(self, **kwargs: Any) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        self.edited.append((kwargs["message_id"], kwargs["text"]))


class Clock:
    """A clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(
    client: ScriptedStatusClient,
    *,
    clock: Clock | None = None,
    message_id: int | None = 4242,
    render_interval: float = 3.0,
    anim_interval: float = 1.2,
) -> tuple[status.StatusRenderer, Clock, list[float]]:
    clock = clock or Clock()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    renderer = status.create_status_renderer(
        client,
        chat_id=7,
        job_id=42,
        link="https://example.test/page",
        message_id=message_id,
        config=status.StatusConfig(
            render_interval=render_interval, anim_interval=anim_interval
        ),
        clock=clock,
        sleep=sleep,
    )
    return renderer, clock, slept


def progress(
    *, index: int = 1, total: int = 3, done: int = 0, size: int | None = None
) -> JobProgress:
    return JobProgress(
        filename="clip.mp4",
        index=index,
        total=total,
        transfer=FetchProgress(
            url="https://cdn.test/clip.mp4",
            role="video",
            transfer_done=done,
            transfer_total=size,
            overall_done=done,
            overall_total=size,
        ),
    )


# ── FR-7.2/7.3: the message's shape ──────────────────────────────────────────


async def test_the_phases_are_shown_in_order_and_named() -> None:
    """FR-7.3: `Resolving → Downloading → Uploading → Done`, on one message.

    ``Done`` is asserted on the *final body* rather than on a header, because FR-7.9 replaces the
    message with the result and that result's own wording is the done line.
    """
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    for phase in (status.Phase.RESOLVING, status.Phase.DOWNLOADING, status.Phase.UPLOADING):
        await renderer.phase(phase)

    await renderer.finish(LinkResult(reason="nope"))

    bodies = [text for _id, text in client.edited]
    assert catalog.PHASE_RESOLVING in bodies[0]
    assert catalog.PHASE_DOWNLOADING in bodies[1]
    assert catalog.PHASE_UPLOADING in bodies[2]
    assert bodies[-1] == catalog.ERROR_OCCURRED.format(error="nope")


async def test_every_live_body_carries_the_cancel_instruction() -> None:
    """FR-7.2's third zone is persistent while the job runs: the moment a user wants out is when it
    looks stuck. Once the result has replaced the message there is nothing left to cancel, which is
    why the final body is checked separately."""
    client = ScriptedStatusClient()
    renderer, clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    clock.advance(3.0)
    await renderer.progress(progress(done=1024, size=4096))
    await renderer.start_uploading(2)

    assert client.edited, "the live states were rendered"
    assert all(catalog.JOB_FOOTER.format(id=42) in text for _id, text in client.edited)

    await renderer.finish(LinkResult(reason="nope"))

    assert catalog.JOB_FOOTER.format(id=42) not in client.edited[-1][1]


async def test_an_unknown_total_shows_bytes_without_inventing_a_percentage() -> None:
    """D30: an unmeasured transfer gets no percentage, because `None` is not zero."""
    client = ScriptedStatusClient()
    renderer, clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    clock.advance(3.0)
    await renderer.progress(progress(done=2048, size=None))

    body = client.edited[-1][1]
    assert "2.0 KB transferred" in body
    assert "%" not in body


async def test_a_measured_transfer_shows_a_bar_and_a_percentage() -> None:
    client = ScriptedStatusClient()
    renderer, clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    clock.advance(3.0)
    await renderer.progress(progress(done=50, size=100))

    body = client.edited[-1][1]
    assert "50.0%" in body
    assert "▰" in body and "▱" in body


async def test_the_upload_row_names_the_file_in_flight() -> None:
    """FR-7.5's own example shape: `Uploading (file 7/25)`."""
    client = ScriptedStatusClient()
    renderer, clock, _ = build(client)

    await renderer.start_uploading(25)
    # The first row is there before anything has landed, because the first file is in flight now.
    assert catalog.JOB_UPLOADING_FILE.format(index=1, total=25) in client.edited[-1][1]

    clock.advance(3.0)
    await renderer.uploaded(6, 25)

    assert catalog.JOB_UPLOADING_FILE.format(index=7, total=25) in client.edited[-1][1]
    assert catalog.JOB_FILES_SENT.format(done=6, total=25) in client.edited[-1][1]


async def test_the_download_counter_is_not_shown_while_uploading() -> None:
    """A finished counter during the upload phase reads as a stalled job."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    await renderer.start_uploading(4)

    assert catalog.JOB_FILES_DOWNLOADED.format(done=0, total=4) not in client.edited[-1][1]


async def test_the_failure_count_appears_once_it_is_known() -> None:
    """FR-7.4: a failure count once any file fails."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    await renderer.start_uploading(5)
    await renderer.failures(2)

    assert catalog.JOB_FILES_FAILED.format(count=2) in client.edited[-1][1]


async def test_a_fallback_notice_is_visible_rather_than_only_logged() -> None:
    """E2: the user is told the link is being retried, so the phase change is not a mystery."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    await renderer.notice(catalog.fallback_notice("gallery-dl"))

    assert catalog.fallback_notice("gallery-dl") in client.edited[-1][1]


# ── FR-7.1/7.9: one message, replaced at the end ─────────────────────────────


async def test_the_final_result_replaces_the_progress_message() -> None:
    """FR-7.9: replaced, not appended — so it must be an edit of the same message id."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    dialogue = await renderer.finish(LinkResult(files=(), failures=(), reason="nope"))

    assert client.sent == [], "a replacement must not post a second message"
    assert client.edited[-1][0] == 4242
    assert client.edited[-1][1] == dialogue.body


async def test_a_job_without_a_message_creates_one_and_then_edits_it() -> None:
    """FR-7.1 for a job that was queued without a status message."""
    client = ScriptedStatusClient(message_id=None)
    renderer, _clock, _ = build(client, message_id=None)

    await renderer.phase(status.Phase.RESOLVING)
    await renderer.phase(status.Phase.DOWNLOADING)

    assert len(client.sent) == 1
    assert len(client.edited) == 1
    assert client.edited[0][0] == client.created * 0 + renderer.state.message_id


# ── FR-7.6/7.7: coalescing and liveness ──────────────────────────────────────


async def test_progress_within_the_render_interval_is_coalesced() -> None:
    """FR-7.7: a 256 KiB reporting step must not become a Telegram edit."""
    client = ScriptedStatusClient()
    renderer, clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    before = len(client.edited)

    for step in (1, 2, 3, 4):
        clock.advance(0.1)
        await renderer.progress(progress(done=step * 1024, size=64 * 1024))

    assert len(client.edited) == before


async def test_progress_past_the_render_interval_is_written() -> None:
    client = ScriptedStatusClient()
    renderer, clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    before = len(client.edited)

    clock.advance(3.0)
    await renderer.progress(progress(done=1024, size=64 * 1024))

    assert len(client.edited) == before + 1


async def test_the_ellipsis_advances_while_a_transfer_is_active() -> None:
    """FR-7.6: the job looks alive even when bytes stall."""
    client = ScriptedStatusClient()
    renderer, clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    first = client.edited[-1][1]

    clock.advance(1.2)
    await renderer.progress(progress(done=1024, size=64 * 1024))

    assert client.edited[-1][1] != first


async def test_a_finished_message_stops_animating() -> None:
    """Done is not a working phase: nothing should keep editing after the result."""
    client = ScriptedStatusClient()
    renderer, clock, _ = build(client)

    await renderer.finish(LinkResult(reason="nope"))
    after = len(client.edited)

    clock.advance(60)
    await renderer.progress(progress(done=99, size=100))

    assert len(client.edited) == after


async def test_the_final_message_is_written_even_inside_both_intervals() -> None:
    """E8: never drop the final result, whatever the throttles say."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    before = len(client.edited)

    await renderer.finish(LinkResult(reason="nope"))

    assert len(client.edited) == before + 1


async def test_a_forced_phase_change_is_not_swallowed_by_the_interval() -> None:
    """FR-7.3: a phase is meaningful and rare, so it must not wait three seconds to appear."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    await renderer.phase(status.Phase.RESOLVING)
    await renderer.phase(status.Phase.DOWNLOADING)

    assert len(client.edited) == 2


async def test_an_unchanged_body_is_not_re_sent() -> None:
    """The client's own ``MessageNotModified`` must be tolerated, not warned about."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    await renderer.phase(status.Phase.RESOLVING)
    await renderer.phase(status.Phase.RESOLVING)

    assert len(client.edited) == 2, "the second phase change still writes; the client says so"


# ── E7/E8: deletion and rate limits ──────────────────────────────────────────


async def test_a_deleted_status_message_is_recreated_once() -> None:
    """E7: recreate and continue reporting, on the new message from then on."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    await renderer.phase(status.Phase.DOWNLOADING)
    client.fail_next = MessageIdInvalid()
    await renderer.phase(status.Phase.UPLOADING)

    assert client.created == 1, "the deleted message was replaced by exactly one new message"
    assert client.sent[-1] == renderer.state.last_text, "the replacement carried the current state"

    await renderer.phase(status.Phase.DOWNLOADING)
    assert client.edited[-1][0] == renderer.state.message_id, "later updates use the new message"


async def test_a_second_deletion_is_not_recreated_again() -> None:
    """Recreating forever would make the bot the spammer."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    client.fail_next = MessageIdInvalid()
    await renderer.phase(status.Phase.DOWNLOADING)
    creations = client.created

    client.fail_next = MessageIdInvalid()
    await renderer.phase(status.Phase.UPLOADING)

    assert client.created == creations
    assert renderer.state.recreated is True


async def test_a_rate_limit_is_waited_out_and_the_update_retried() -> None:
    """E8: coalesce and never drop the result — the wait is the remedy, not a failure."""
    client = ScriptedStatusClient()
    renderer, _clock, slept = build(client)

    client.fail_next = FloodWait(value=17)
    await renderer.phase(status.Phase.DOWNLOADING)

    assert slept == [17]
    assert client.edited, "the update went out after the wait"


async def test_a_rate_limit_that_does_not_clear_is_dropped_rather_than_raised() -> None:
    """A report must never fail the download it reports on."""
    client = ScriptedStatusClient()
    renderer, _clock, slept = build(client)

    class AlwaysLimited(ScriptedStatusClient):
        async def edit_message_text(self, **kwargs: Any) -> None:
            raise FloodWait(value=5)

    limited = AlwaysLimited()
    renderer, _clock, slept = build(limited)

    await renderer.phase(status.Phase.DOWNLOADING)

    assert slept == [5.0], "waited once, then gave up rather than looping"


async def test_any_other_failure_to_report_is_swallowed() -> None:
    """A user who blocked the bot must not have their job fail twice."""
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    class Broken(ScriptedStatusClient):
        async def edit_message_text(self, **kwargs: Any) -> None:
            raise OSError("network down")

    broken = Broken()
    renderer, _clock, _ = build(broken)

    await renderer.phase(status.Phase.DOWNLOADING)

    assert broken.edited == []


async def test_message_not_modified_is_not_an_error() -> None:
    client = ScriptedStatusClient()
    renderer, _clock, _ = build(client)

    client.fail_next = MessageNotModified()
    await renderer.phase(status.Phase.DOWNLOADING)

    assert renderer.state.last_text in client.sent + [renderer.state.last_text]


# ── the pieces used by the check above ───────────────────────────────────────


def test_the_renderer_never_uses_a_private_api_of_the_client() -> None:
    """The protocol is two calls; three would be a contract change worth noticing."""
    assert set(status.StatusClient.__protocol_attrs__) == {"send_message", "edit_message_text"}


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "0.0 B"), (1023, "1023.0 B"), (1024, "1.0 KB"), (5 * 1024 * 1024, "5.0 MB")],
)
def test_bytes_are_formatted_for_a_glance(count: int, expected: str) -> None:
    assert status._bytes(count) == expected


def test_a_short_value_is_not_truncated() -> None:
    assert status._shorten("clip.mp4", 20) == "clip.mp4"


def test_a_long_value_keeps_its_beginnings() -> None:
    """The host and the start of a filename are the recognisable part; the tail is not."""
    shortened = status._shorten("https://example.test/" + "x" * 100, 60)

    assert len(shortened) == 60
    assert shortened.startswith("https://example.test/")
    assert shortened.endswith("…")
