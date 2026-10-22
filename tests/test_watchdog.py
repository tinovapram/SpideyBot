"""Watchdogs: loop-lag detection and Telegram connection recovery."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from core import sessions, watchdog


class FakeClock:
    """Virtual clock plus a sleep that advances it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.slept += delay
        self.now += delay
        await asyncio.sleep(0)


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def names(self) -> list[str]:
        return [event for event, _ in self.events]


class _FakeBot:
    """Connection state the test can flip at will."""

    def __init__(self, connected: bool = False) -> None:
        self.connected = connected
        self.connects = 0
        self.fail = False

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connects += 1
        if self.fail:
            raise ConnectionError("boom")
        self.connected = True


class _FakeClient:
    def __init__(self, connected: bool) -> None:
        self.connected = connected
        self.disconnects = 0
        self.session = SimpleNamespace(closed=False)

        def close() -> None:
            self.session.closed = True

        self.session.close = close

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1


# ── loop lag ─────────────────────────────────────────────────────


async def test_a_blocked_loop_is_reported_and_recovery_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = RecordingLogger()
    monkeypatch.setattr(watchdog, "logger", log)
    clock = FakeClock()

    async def sleep(delay: float) -> None:
        # The sleep completes, but 9 s of wall clock disappear around it.
        clock.now += delay
        await asyncio.sleep(0)

    task = asyncio.create_task(
        watchdog.monitor_loop_lag(threshold=2.0, interval=0.5, clock=clock, sleep=sleep)
    )
    await asyncio.sleep(0.02)  # one healthy tick

    clock.now += 9.0  # pretend a synchronous call held the loop
    await asyncio.sleep(0.02)
    await asyncio.sleep(0.02)  # next tick is healthy again

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "event loop blocked" in log.names()
    assert "event loop recovered" in log.names()


async def test_a_healthy_loop_stays_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    log = RecordingLogger()
    monkeypatch.setattr(watchdog, "logger", log)
    clock = FakeClock()

    async def sleep(delay: float) -> None:
        clock.now += delay
        await asyncio.sleep(0)

    task = asyncio.create_task(
        watchdog.monitor_loop_lag(threshold=5.0, interval=0.01, clock=clock, sleep=sleep)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert log.names() == []


async def test_a_long_block_is_reported_periodically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = RecordingLogger()
    monkeypatch.setattr(watchdog, "logger", log)
    clock = FakeClock()

    async def sleep(delay: float) -> None:
        clock.now += 40.0  # every tick is massively late
        await asyncio.sleep(0)

    task = asyncio.create_task(
        watchdog.monitor_loop_lag(threshold=2.0, interval=0.5, clock=clock, sleep=sleep)
    )
    for _ in range(3):
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    names = log.names()
    assert names[0] == "event loop blocked"
    assert "event loop still blocked" in names


# ── connection watchdog ──────────────────────────────────────────


@pytest.fixture
def clean_sessions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sessions, "_active_clients", {})
    monkeypatch.setattr(sessions, "_last_used", {})
    monkeypatch.setattr(sessions, "_leases", {})
    monkeypatch.setattr(sessions, "_tiers", {})
    monkeypatch.setattr(sessions, "_disconnected_since", {})
    yield


async def test_bot_reconnects_once_the_grace_expires(
    monkeypatch: pytest.MonkeyPatch, clean_sessions
) -> None:
    log = RecordingLogger()
    monkeypatch.setattr(watchdog, "logger", log)
    clock = FakeClock()
    bot = _FakeBot(connected=False)

    task = asyncio.create_task(
        watchdog.watch_connections(
            bot, interval=10, grace=30, max_failures=5, clock=clock, sleep=clock.sleep
        )
    )
    for _ in range(5):  # 10, 20, 30 (grace reached), 40 -> reconnect
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bot.connects >= 1
    assert bot.connected is True
    assert "bot disconnected, reconnecting" in log.names()


async def test_a_healthy_bot_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, clean_sessions
) -> None:
    log = RecordingLogger()
    monkeypatch.setattr(watchdog, "logger", log)
    clock = FakeClock()
    bot = _FakeBot(connected=True)

    task = asyncio.create_task(
        watchdog.watch_connections(
            bot, interval=10, grace=30, max_failures=5, clock=clock, sleep=clock.sleep
        )
    )
    for _ in range(3):
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bot.connects == 0
    assert log.names() == []


async def test_missing_bot_is_a_noop() -> None:
    await watchdog.watch_connections(None, interval=1, grace=1, max_failures=1)


# ── user-client pruning ─────────────────────────────────────────


async def test_disconnected_user_client_is_dropped_after_grace(
    monkeypatch: pytest.MonkeyPatch, clean_sessions
) -> None:
    dead = _FakeClient(connected=False)
    healthy = _FakeClient(connected=True)
    monkeypatch.setattr(sessions, "_active_clients", {1: dead, 2: healthy})

    # First pass starts the clock; nothing may be dropped yet.
    assert await sessions.drop_disconnected_clients(grace_seconds=60) == 0
    assert 1 in sessions._active_clients

    sessions._disconnected_since[1] = time.monotonic() - 120
    assert await sessions.drop_disconnected_clients(grace_seconds=60) == 1

    assert 1 not in sessions._active_clients
    assert dead.disconnects == 1
    assert dead.session.closed is True  # the SQLite handle is released
    assert 2 in sessions._active_clients


async def test_leased_user_client_is_never_pruned(
    monkeypatch: pytest.MonkeyPatch, clean_sessions
) -> None:
    dead = _FakeClient(connected=False)
    monkeypatch.setattr(sessions, "_active_clients", {3: dead})
    monkeypatch.setattr(sessions, "_leases", {3: 1})
    monkeypatch.setattr(sessions, "_disconnected_since", {3: 0.0})

    assert await sessions.drop_disconnected_clients(grace_seconds=0) == 0
    assert 3 in sessions._active_clients
