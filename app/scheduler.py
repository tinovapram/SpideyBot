"""Periodic housekeeping: recovering leases, and pruning what has aged out (D18, D21).

Two jobs on two clocks, because they answer different failures:

* the **lease sweep** runs often. It is the only way a worker that died mid-download is ever
  noticed: nothing else can tell a slow job from an abandoned one (FR-10.5, E26).
* **retention** runs rarely. It deletes finished jobs past ``download.retention_days`` — the
  only thing keeping the database from growing without bound, since finished jobs are otherwise
  never removed.

**There is deliberately no "daily reset" step.** Usage is stored as one row per user per day, so
a new day *is* an empty allowance: the boundary is a property of the schema rather than a task
that has to fire on time. A scheduled reset would be a second implementation of the same rule,
and the one that is wrong whenever the process happened to be down at midnight. The equivalent
guarantee is tested instead (see ``tests/integration/test_scheduler.py``), and the clock is
injected so the boundary is controllable without waiting for one (D18).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select

from app import db as db_module
from app import paths, queue
from app.config import Settings
from app.models import Job

log = logging.getLogger(__name__)

#: Bounds how long an abandoned job waits before it is retried. The sweep only acts on leases
#: that have *already* expired, so this affects detection latency and nothing else.
DEFAULT_SWEEP_INTERVAL = 30.0

#: Retention is a cheap `DELETE` over an indexed column, so it does not need to be frequent.
DEFAULT_RETENTION_INTERVAL = 3600.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Housekeeping:
    """What one pass did, so a caller can log it rather than guess."""

    reclaimed: queue.Reclaim = field(default_factory=queue.Reclaim)
    pruned_jobs: tuple[int, ...] = ()
    pruned_dirs: tuple[Path, ...] = ()


async def sweep_leases(settings: Settings, *, now: datetime | None = None) -> queue.Reclaim:
    """Requeue (or retire) jobs whose worker stopped renewing its lease."""
    async with db_module.session() as session:
        return await queue.reclaim_expired(session, settings, now=now)


async def prune_history(settings: Settings, *, now: datetime | None = None) -> tuple[int, ...]:
    """Delete finished jobs older than the retention window. Returns the ids removed.

    Only terminal jobs are eligible, and only by ``finished_at``: a running job has no
    ``finished_at`` at all, so it can never be selected by accident. ``retention_days = 0``
    prunes every finished job, which is the literal reading of "how long history is kept".
    """
    moment = now or _utcnow()
    cutoff = moment - timedelta(days=settings.download.retention_days)

    statement = (
        delete(Job)
        .where(
            Job.status.in_(tuple(queue.TERMINAL_STATES)),
            Job.finished_at.is_not(None),
            Job.finished_at < cutoff,
        )
        .returning(Job.id)
    )
    async with db_module.session() as session:
        removed = tuple((await session.execute(statement)).scalars().all())

    for job_id in removed:
        _discard(paths.job_tmp_dir(job_id))
    return removed


def _discard(directory: Path) -> bool:
    """Remove a staging directory if it is there. Absent is the normal case, not an error."""
    if not directory.exists():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


async def orphaned_temp_dirs() -> tuple[Path, ...]:
    """Staging directories with no live job behind them, as left by a hard kill.

    A crash between "create the directory" and "record the job" leaves one behind, and so does a
    job whose terminal-state cleanup never ran. Running this at startup is what stops
    ``data/tmp`` from accumulating them across restarts (D21).
    """
    if not paths.TMP_DIR.exists():
        return ()

    async with db_module.session() as session:
        result = await session.execute(select(Job.id))
        live = {str(job_id) for job_id in result.scalars().all()}

    return tuple(
        entry
        for entry in sorted(paths.TMP_DIR.iterdir())
        if entry.is_dir() and entry.name not in live
    )


async def sweep_orphans() -> tuple[Path, ...]:
    """Delete orphaned staging directories. Returns what was removed."""
    orphans = await orphaned_temp_dirs()
    for directory in orphans:
        _discard(directory)
    return orphans


async def housekeeping(settings: Settings, *, now: datetime | None = None) -> Housekeeping:
    """One full pass: recover leases, then prune aged-out history."""
    moment = now or _utcnow()
    reclaimed = await sweep_leases(settings, now=moment)
    pruned = await prune_history(settings, now=moment)
    return Housekeeping(reclaimed=reclaimed, pruned_jobs=pruned)


class Scheduler:
    """The housekeeping loop (D18).

    The clock is injectable so the retention decision can be driven past a boundary in a test
    instead of by waiting an hour, and so "did a day change?" is answered from one place.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL,
        retention_interval: float = DEFAULT_RETENTION_INTERVAL,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._sweep_interval = sweep_interval
        self._retention_interval = retention_interval
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._next_retention: datetime | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._next_retention = self._clock() + timedelta(seconds=self._retention_interval)
        self._task = asyncio.create_task(self._loop(), name="spideybot-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def tick(self) -> Housekeeping:
        """One pass, callable directly so tests need not race the loop."""
        moment = self._clock()
        reclaimed = await sweep_leases(self._settings, now=moment)

        pruned: tuple[int, ...] = ()
        if self._next_retention is None or moment >= self._next_retention:
            pruned = await prune_history(self._settings, now=moment)
            self._next_retention = moment + timedelta(seconds=self._retention_interval)

        if reclaimed.total or pruned:
            log.info(
                "housekeeping: %d job(s) recovered, %d pruned",
                reclaimed.total,
                len(pruned),
            )
        return Housekeeping(reclaimed=reclaimed, pruned_jobs=pruned)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - housekeeping must not end the process
                log.exception("housekeeping pass failed; retrying at the next interval")
            await asyncio.sleep(self._sweep_interval)
