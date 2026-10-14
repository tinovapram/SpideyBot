"""The startup and shutdown order (P5's wiring).

The order in :mod:`app.runtime` is the contract, so it is asserted rather than described: a handler
must not reply before the dispatcher is registered, a worker must not claim a Telegram job before
the pool exists, and shutdown must close the clients *after* the workers have stopped using them.
Each of those is a bug that only shows up under load — or, worse, only in production — so the test
drives ``serve`` with fakes and reads the sequence.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app import capabilities, config, db, dbprovider, paths, runtime, scheduler, workers
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
    # The database is a collaborator like any other: reached through its own modules, and
    # stubbed so this file is about start-up order rather than about Postgres.
    monkeypatch.setattr(dbprovider, "is_initialised", lambda: True)
    monkeypatch.setattr(dbprovider, "is_running", lambda: True)
    monkeypatch.setattr(db, "configure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db, "require_current_schema", _schema_is_current)
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


async def _schema_is_current() -> None:
    return None


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


# ── the database at start-up ────────────────────────────────────────────


async def test_a_missing_cluster_names_the_command_that_creates_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh checkout must not fail with a connection error three layers down."""
    monkeypatch.setattr(dbprovider, "uses_bundled_cluster", lambda _settings: True)
    monkeypatch.setattr(dbprovider, "is_initialised", lambda: False)

    with pytest.raises(dbprovider.DbProviderError) as raised:
        await runtime._prepare_database(settings)

    assert "db init" in str(raised.value)


async def test_a_stopped_cluster_is_started_but_a_running_one_is_left_alone(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``db start`` is idempotent, and invoking it on every run would be wasted work."""
    started: list[str] = []
    monkeypatch.setattr(dbprovider, "uses_bundled_cluster", lambda _settings: True)
    monkeypatch.setattr(dbprovider, "is_initialised", lambda: True)
    monkeypatch.setattr(dbprovider, "start", lambda _settings: started.append("start"))
    monkeypatch.setattr(db, "configure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db, "require_current_schema", _schema_is_current)

    monkeypatch.setattr(dbprovider, "is_running", lambda: False)
    await runtime._prepare_database(settings)
    assert started == ["start"]

    monkeypatch.setattr(dbprovider, "is_running", lambda: True)
    await runtime._prepare_database(settings)
    assert started == ["start"], "a running cluster must not be started a second time"


async def test_an_external_database_is_never_started_locally(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With SPIDEY_DB_PASSWORD set the bundled cluster is none of this process's business."""
    monkeypatch.setattr(dbprovider, "uses_bundled_cluster", lambda _settings: False)

    def explode() -> bool:
        raise AssertionError("the local cluster must not be touched")

    monkeypatch.setattr(dbprovider, "is_initialised", explode)
    monkeypatch.setattr(db, "configure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db, "require_current_schema", _schema_is_current)

    await runtime._prepare_database(settings)


async def test_the_engine_is_configured_before_the_schema_is_checked(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check needs an engine; checking first would report the wrong problem."""
    sequence: list[str] = []
    monkeypatch.setattr(dbprovider, "is_initialised", lambda: True)
    monkeypatch.setattr(dbprovider, "is_running", lambda: True)
    monkeypatch.setattr(db, "configure", lambda *_args, **_kwargs: sequence.append("configure"))

    async def check() -> None:
        sequence.append("check")

    monkeypatch.setattr(db, "require_current_schema", check)

    await runtime._prepare_database(settings)

    assert sequence == ["configure", "check"]


async def test_a_schema_behind_head_names_the_command_that_fixes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "head_revision", lambda: "head123")

    async def applied() -> set[str]:
        return {"old456"}

    monkeypatch.setattr(db, "applied_revisions", applied)

    with pytest.raises(db.SchemaOutOfDate) as raised:
        await db.require_current_schema()

    message = str(raised.value)
    assert "db migrate" in message
    assert "old456" in message and "head123" in message


async def test_a_database_with_no_schema_is_reported_as_unmigrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``alembic_version`` table is the fresh-database case, not an obscure error."""
    monkeypatch.setattr(db, "head_revision", lambda: "head123")

    async def applied() -> set[str]:
        return set()

    monkeypatch.setattr(db, "applied_revisions", applied)

    with pytest.raises(db.SchemaOutOfDate) as raised:
        await db.require_current_schema()

    assert "no migrations applied" in str(raised.value)


async def test_a_database_at_head_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check must not fire on a healthy database, or every start would fail."""
    monkeypatch.setattr(db, "head_revision", lambda: "head123")

    async def applied() -> set[str]:
        return {"head123"}

    monkeypatch.setattr(db, "applied_revisions", applied)

    await db.require_current_schema()


# ── the capability report ───────────────────────────────────────────────


class MissingCapability(FakeCapability):
    """The report's whole reason to exist: a capability that is not there."""

    ok = False

    def __init__(self, name: str, *, required: bool) -> None:
        self.name = name
        self.required = required
        self.detail = "missing in this test"


async def test_a_missing_capability_is_reported_rather_than_fatal(
    wired, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Reporting a missing capability must not take startup down with it.

    The report is the one log line that only runs on a machine already in trouble, so a bug
    in it hides exactly the failures it exists to announce. ``name`` is a reserved
    ``LogRecord`` attribute: passing it through ``extra`` made stdlib logging raise
    ``KeyError: "Attempt to overwrite 'name' in LogRecord"``, so a Linux host without the
    optional fast event loop failed to start instead of logging a warning about it.
    """
    monkeypatch.setattr(
        capabilities,
        "probe",
        lambda: [
            MissingCapability("ffmpeg", required=True),
            MissingCapability("uvloop", required=False),
        ],
    )
    caplog.set_level(logging.INFO)

    await runtime.serve(wired)

    # The name has to reach the text, not just the record: the stdlib handler formats
    # %(message)s only, so a warning without it says nothing about what is missing.
    assert "required capability missing: ffmpeg" in caplog.text
    assert "optional capability absent: uvloop" in caplog.text
    reported = {record.capability for record in caplog.records if hasattr(record, "capability")}
    assert reported == {"ffmpeg", "uvloop"}
