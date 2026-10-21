"""Send gate: media tokens, media slots, chat pacing, retries and registry."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import types
from telethon.errors import FileReferenceExpiredError, FloodWaitError

from core import sendgate
from core.sendgate import FileExpiredError, SendGate, TokenBucket
from utils.files import prepare_media
from utils.telethon import safe_send_file


class FakeClock:
    """Virtual clock: sleeping advances time instead of blocking."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay
        await asyncio.sleep(0)


def make_gate(
    clock: FakeClock,
    *,
    media_per_minute: int = 6000,
    media_burst: int = 600,
    slots: int = 2,
    chat_per_minute: int = 6000,
    send_burst: int = 600,
    jitter: float = 0.0,
    attempts: int = 4,
    client: Any | None = None,
) -> SendGate:
    return SendGate(
        "test",
        media_slots=slots,
        media_per_minute=media_per_minute,
        media_burst=media_burst,
        send_chat_per_minute=chat_per_minute,
        send_burst=send_burst,
        jitter_seconds=jitter,
        max_attempts=attempts,
        client=client,
        clock=clock,
        sleep=clock.sleep,
    )


# ── Token bucket ─────────────────────────────────────────────────


async def test_burst_is_capped_by_capacity_not_rate() -> None:
    clock = FakeClock()
    bucket = TokenBucket(60, burst=5, clock=clock, sleep=clock.sleep)

    assert [await bucket.take() for _ in range(5)] == [0.0] * 5
    assert await bucket.take() == pytest.approx(1.0)  # rate is 60/min


async def test_media_tokens_pace_preparation() -> None:
    clock = FakeClock()
    gate = make_gate(clock, media_per_minute=2, media_burst=2)
    marks: list[float] = []

    async def prepare() -> None:
        await gate.take_media()
        marks.append(clock.now)

    await asyncio.gather(*(prepare() for _ in range(3)))

    assert sum(1 for mark in marks if mark - 1000.0 < 1.0) == 2  # burst of 2
    assert marks[-1] - 1000.0 >= 29.0  # third waits for a refill


async def test_media_tokens_are_shared_by_all_chats() -> None:
    clock = FakeClock()
    gate = make_gate(clock, media_per_minute=2, media_burst=2)

    await gate.take_media()  # media bound for chat A
    await gate.take_media()  # media bound for chat B
    assert clock.now == 1000.0

    await gate.take_media()  # one client budget, regardless of chat
    assert clock.now - 1000.0 >= 29.0


# ── Chat pacing (per message RPC, not per media) ─────────────────


async def test_chat_bucket_paces_a_burst_of_messages() -> None:
    clock = FakeClock()
    gate = make_gate(clock, chat_per_minute=2, send_burst=2)
    marks: list[float] = []

    async def op() -> None:
        marks.append(clock.now)

    await asyncio.gather(*(gate.send(7, op) for _ in range(3)))

    assert len(marks) == 3
    assert sum(1 for mark in marks if mark - 1000.0 < 1.0) == 2
    assert marks[-1] - 1000.0 >= 29.0


async def test_chat_buckets_are_independent() -> None:
    clock = FakeClock()
    gate = make_gate(clock, chat_per_minute=2, send_burst=2)
    marks: list[tuple[int, float]] = []

    def operation(chat: int):
        async def op() -> None:
            marks.append((chat, clock.now))
        return op

    await gate.send(1, operation(1))
    await gate.send(1, operation(1))  # chat 1's bucket is now empty
    before = clock.now

    await gate.send(2, operation(2))
    assert clock.now == before  # chat 2 is unaffected by chat 1

    await gate.send(1, operation(1))
    assert clock.now - before >= 29.0  # chat 1 waits for its own refill


# ── Flood cooldown ───────────────────────────────────────────────


async def test_flood_wait_sets_a_shared_cooldown() -> None:
    clock = FakeClock()
    gate = make_gate(clock, attempts=1)
    marks: list[float] = []

    async def flood() -> None:
        marks.append(clock.now)
        raise FloodWaitError(request=None, capture=5)

    async def ok() -> str:
        marks.append(clock.now)
        return "sent"

    with pytest.raises(FloodWaitError):
        await gate.send(1, flood)

    assert gate.blocked_for == pytest.approx(5.0)
    assert await gate.send(1, ok) == "sent"
    assert marks[-1] - marks[0] >= 5.0
    assert gate.blocked_for == 0.0


async def test_flood_retries_up_to_the_attempt_limit() -> None:
    clock = FakeClock()
    gate = make_gate(clock, attempts=3)
    attempts = 0

    async def always_flood() -> None:
        nonlocal attempts
        attempts += 1
        raise FloodWaitError(request=None, capture=1)

    with pytest.raises(FloodWaitError):
        await gate.send(1, always_flood)

    assert attempts == 3


async def test_cooldown_expires_once_for_all_waiters() -> None:
    clock = FakeClock()
    gate = make_gate(clock)
    gate._block(4)  # noqa: SLF001 - exercising the cooldown directly

    async def op() -> str:
        return "ok"

    results = await asyncio.gather(*(gate.send(chat, op) for chat in range(5)))

    assert results == ["ok"] * 5
    assert clock.now >= 1004.0


# ── Retry taxonomy ───────────────────────────────────────────────


class _SendFileClient:
    """Fails the first *failures* sends with ``error``, then succeeds."""

    def __init__(self, failures: int, error: Exception) -> None:
        self.failures = failures
        self.error = error
        self.sent: list[Any] = []

    async def send_file(self, chat_id: Any, media: Any, **_kwargs: Any) -> str:
        self.sent.append(media)
        if self.failures:
            self.failures -= 1
            raise self.error
        return "message"


async def test_transient_error_resends_the_same_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    gate = make_gate(clock)
    monkeypatch.setattr(sendgate, "get_gate", lambda _client: gate)
    client = _SendFileClient(1, TimeoutError("slow"))
    media = object()

    await safe_send_file(client, 42, media, caption="x")

    assert client.sent == [media, media]  # re-sent, not re-uploaded
    assert clock.now - 1000.0 >= 2.0  # exponential backoff


async def test_expired_file_reference_fails_fast() -> None:
    clock = FakeClock()
    gate = make_gate(clock)
    attempts = 0

    async def expired() -> None:
        nonlocal attempts
        attempts += 1
        raise FileReferenceExpiredError(request=None)

    with pytest.raises(FileExpiredError):
        await gate.send(1, expired)

    assert attempts == 1


async def test_album_batch_with_expired_media_is_dropped_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from utils.telethon import send_album

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def send_file(self, chat_id: Any, media: Any, **_kwargs: Any) -> None:
            self.calls += 1
            raise FileExpiredError("expired")

    monkeypatch.setattr(sendgate, "get_gate", lambda _client: make_gate(FakeClock()))
    client = Client()

    def handle() -> Any:
        return object()

    photos = [
        types.InputMediaUploadedPhoto(file=handle()),
        types.InputMediaUploadedPhoto(file=handle()),
    ]

    assert await send_album(client, 1, photos, ["", ""]) == 0
    assert client.calls == 1  # no per-item retry storm


# ── Media slots ──────────────────────────────────────────────────


async def test_media_slots_bound_concurrency() -> None:
    gate = make_gate(FakeClock(), slots=2)
    current = 0
    peak = 0

    async def prepare() -> None:
        nonlocal current, peak
        async with gate.media_slot():
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.01)
            current -= 1

    await asyncio.gather(*(prepare() for _ in range(6)))

    assert peak == 2
    assert gate.media_in_flight == 0


async def test_raising_the_budget_wakes_a_waiter() -> None:
    gate = make_gate(FakeClock(), slots=1)
    started = asyncio.Event()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def first() -> None:
        async with gate.media_slot():
            started.set()
            await release.wait()

    async def second() -> None:
        async with gate.media_slot():
            entered.set()

    holder = asyncio.create_task(first())
    await started.wait()
    waiter = asyncio.create_task(second())
    await asyncio.sleep(0.01)
    assert not entered.is_set()  # blocked by the single slot

    await gate.set_media_slots(2)
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    release.set()
    await asyncio.gather(holder, waiter)


# ── prepare_media integration ────────────────────────────────────


async def test_prepare_media_costs_one_token_and_one_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A file plus its thumbnail is still one media: one token, one slot."""
    clock = FakeClock()
    gate = make_gate(clock, media_per_minute=1, media_burst=1, slots=1)
    monkeypatch.setattr(sendgate, "get_gate", lambda _client: gate)
    monkeypatch.setattr(
        "telethon.utils.get_attributes", lambda path, **_kw: ([], "video/mp4")
    )

    class Client:
        def __init__(self) -> None:
            self.uploads: list[str] = []
            self.current = 0
            self.peak = 0

        async def upload_file(self, path: str, **_kwargs: Any) -> object:
            self.uploads.append(path)
            self.current += 1
            self.peak = max(self.peak, self.current)
            await asyncio.sleep(0.01)
            self.current -= 1
            return object()

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"jpeg")

    client = Client()
    media = await prepare_media(client, str(video), thumb=str(thumb))

    assert isinstance(media, types.InputMediaUploadedDocument)
    assert client.uploads == [str(video), str(thumb)]
    assert client.peak == 1  # both uploads inside the same slot
    assert clock.now == 1000.0  # the single token was available


async def test_prepare_media_paces_each_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    clock = FakeClock()
    gate = make_gate(clock, media_per_minute=2, media_burst=2, slots=4)
    monkeypatch.setattr(sendgate, "get_gate", lambda _client: gate)

    class Client:
        async def upload_file(self, path: str, **_kwargs: Any) -> object:
            return object()

    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"jpeg")
        paths.append(str(path))

    client = Client()
    await asyncio.gather(*(prepare_media(client, path) for path in paths))

    # Three photos cost three media tokens, whatever the delivery grouping.
    assert clock.now - 1000.0 >= 29.0


# ── Registry ─────────────────────────────────────────────────────


@pytest.fixture
def clean_registry():
    sendgate.reset()
    yield
    sendgate.reset()


def test_gates_are_per_client(clean_registry) -> None:
    first = SimpleNamespace(name="a")
    second = SimpleNamespace(name="b")

    assert sendgate.get_gate(first) is sendgate.get_gate(first)
    assert sendgate.get_gate(first) is not sendgate.get_gate(second)


def test_labelled_clients_and_configured_budget(clean_registry) -> None:
    from core.config import get_settings

    client = SimpleNamespace()
    sendgate.label_client(client, "bot")
    gate = sendgate.get_gate(client)
    settings = get_settings()

    assert gate.label == "bot"
    assert gate.media_limit == settings.media_slot_budget() == 5
    assert settings.media_per_minute == 60


def test_media_budget_can_follow_the_sessionless_share() -> None:
    from core.config import Settings

    settings = Settings.model_construct(media_concurrency=0, sessionless_worker_slots=7)

    assert settings.media_slot_budget() == 7


def test_prune_buckets_forgets_idle_chats() -> None:
    clock = FakeClock()
    gate = make_gate(clock)
    old = TokenBucket(10, clock=clock, sleep=clock.sleep)
    old.last_used = 1000.0
    clock.now = 2000.0
    fresh = TokenBucket(10, clock=clock, sleep=clock.sleep)
    gate._chat_buckets.update({1: old, 2: fresh})  # noqa: SLF001

    assert gate.prune_buckets(idle_seconds=100) == 1
    assert list(gate._chat_buckets) == [2]  # noqa: SLF001


def test_sweep_drops_disconnected_clients(clean_registry) -> None:
    client = SimpleNamespace(is_connected=lambda: True)
    gate = sendgate.get_gate(client)

    client.is_connected = lambda: False

    assert sendgate.sweep() == 1
    assert sendgate.get_gate(client) is not gate
