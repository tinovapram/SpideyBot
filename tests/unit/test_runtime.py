"""The startup and shutdown order (P5's wiring).

The order in :mod:`app.runtime` is the contract, so it is asserted rather than described: a handler
must not reply before the dispatcher is registered, a worker must not claim a Telegram job before
the pool exists, and shutdown must close the clients *after* the workers have stopped using them.
Each of those is a bug that only shows up under load — or, worse, only in production — so the test
drives ``serve`` with fakes and reads the sequence.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import capabilities, config, paths, runtime, scheduler, workers
from app.config import Settings
from telegram import clients, sessions

ORDER: list[str] = []


class FakeBot:
    async def start(self) -> None:
        ORDER.append("bot.start")

    async def stop(self) -> None:
        ORDER.append("bot.stop")


class FakePool(sessions.SessionPool):
    """A pool that records being installed and closed without connecting anything."""

    def __init__(self, settings: Settings) -> None:  # noqa: D107 - see the class docstring
        super().__init__(settings, factory=lambda _user_id: FakeBot())

    async def close_all(self) -> None:
        ORDER.append("pool.close")


class FakeWorkers(workers.WorkerPool):
    def __init__(self, settings: Settings, handler: Any, *, size: int | None = None) -> None:
        super().__init__(settings, handler, size=size)
        ORDER.append("workers.init")

    async def start(self) -> None:
        ORDER.append("workers.start")

    async def stop(self) -> None:
        ORDER.append("workers.stop")


class FakeScheduler(scheduler.Scheduler):
    def __init__(self, settings: Settings, *, sessions_pool: Any = None) -> None:
        super().__init__(settings, sessions_pool=sessions_pool)
        ORDER.append("scheduler.init")

    async def start(self) -> None:
        ORDER.append("scheduler.start")

    async def stop(self) -> None:
        ORDER.append("scheduler.stop")


class FakeCapability:
    ok = True
    required = True
    name = "ffmpeg"
    detail = "fake"


@pytest.fixture
def wired(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """``serve`` with everything outside the process replaced.

    The collaborators are patched on their *own* modules, because that is how ``runtime`` reaches
    them (``sessions.SessionPool``, ``workers.WorkerPool``) — patching names into ``runtime`` itself
    would test a layout the module does not have.
    """
    ORDER.clear()
    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(paths, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(paths, "insecure_secret_dirs", lambda: [])
    monkeypatch.setattr(capabilities, "probe", lambda: [FakeCapability()])
    monkeypatch.setattr(clients, "build_bot", lambda _settings: FakeBot())
    monkeypatch.setattr(sessions, "SessionPool", FakePool)
    monkeypatch.setattr(workers, "WorkerPool", FakeWorkers)
    monkeypatch.setattr(scheduler, "Scheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "sweep_orphans", _no_orphans)
    monkeypatch.setattr(sessions, "set_pool", _record_pool)

    async def idle() -> None:
        ORDER.append("idle")

    monkeypatch.setattr(runtime, "idle", idle)
    monkeypatch.setattr(runtime.app_logging, "setup_logging", lambda **_kwargs: _Redactor())
    return settings


class _Redactor:
    count = 0


async def _no_orphans() -> tuple[Any, ...]:
    return ()


def _record_pool(pool: Any) -> None:
    ORDER.append("pool.install" if pool is not None else "pool.clear")


async def test_serve_starts_and_stops_in_the_documented_order(wired) -> None:
    await runtime.serve(wired)

    # The starts, in order: the pool exists before the client starts (a handler's first job may ask
    # for a session the moment a claim lands), the client starts before any worker, and the
    # scheduler starts last, because reaping a pool nothing is using yet would be pointless.
    for earlier, later in (
        ("pool.install", "bot.start"),
        ("bot.start", "workers.start"),
        ("workers.start", "scheduler.start"),
        ("scheduler.start", "idle"),
    ):
        assert ORDER.index(earlier) < ORDER.index(later), f"{earlier} must precede {later}"

    # And the shutdown, in reverse: no job may still be running when its clients go away.
    for earlier, later in (
        ("idle", "workers.stop"),
        ("workers.stop", "scheduler.stop"),
        ("scheduler.stop", "pool.close"),
        ("pool.close", "bot.stop"),
    ):
        assert ORDER.index(earlier) < ORDER.index(later), f"{earlier} must precede {later}"
    assert ORDER[-1] == "bot.stop", "the client is the last thing to go"

async def test_serve_can_be_driven_with_a_small_pool(wired) -> None:
    """So a test or a small deployment can start one worker without changing the settings."""
    await runtime.serve(wired, pool_size=1)

    assert "workers.init" in ORDER


async def test_the_client_is_started_before_any_worker_can_claim_a_job(wired) -> None:
    """A job claimed before the client is up would fail to post its own status message."""
    await runtime.serve(wired)

    assert ORDER.index("bot.start") < ORDER.index("workers.start")
    assert ORDER.index("pool.install") < ORDER.index("workers.start"), (
        "a Telegram job asks the pool for its session the moment it is claimed"
    )
