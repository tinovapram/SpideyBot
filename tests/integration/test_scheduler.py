"""Housekeeping: recovering abandoned jobs, pruning history, and the daily boundary.

The daily boundary is tested here rather than implemented in the scheduler, because that is the
design: usage is day-keyed, so "reset" is a property of the schema. A test is what keeps that
claim honest as the schema changes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app import db, paths, queue, quota, scheduler, users
from app.config import Settings
from app.models import Job

LINK = "https://example.test/album/1"
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _with_settings(settings: Settings, *, retention_days: int) -> Settings:
    return settings.model_copy(
        update={"download": settings.download.model_copy(update={"retention_days": retention_days})}
    )


async def _register(*user_ids: int) -> None:
    async with db.session() as session:
        for user_id in user_ids:
            await users.upsert(session, user_id=user_id, first_name="T")


async def _finish_a_job(user_id: int, settings, *, status: str, at: datetime) -> int:
    """A job that reached a terminal state at a chosen moment."""
    async with db.session() as session:
        job = await queue.enqueue(session, user_id=user_id, link=LINK, tier="free", priority=2.0)
        claimed = await queue.claim(session, settings)
        assert claimed is not None
        await queue.terminate(session, claimed, status, now=at)
        return job.id


# ── the daily boundary (spec 8.5) ───────────────────────────────────────


async def test_a_new_day_starts_with_a_fresh_allowance(clean, settings) -> None:
    """No reset job exists; a new day is a new row. This is that claim, tested."""
    await _register(1)
    first_day = quota.today(NOW)
    async with db.session() as session:
        for _ in range(3):
            await quota.reserve_download(session, 1, limit=3, day=first_day)

    async with db.session() as session:
        spent = await quota.reserve_download(session, 1, limit=3, day=first_day)
        tomorrow = await quota.reserve_download(
            session, 1, limit=3, day=first_day + timedelta(days=1)
        )

    assert spent is False, "the first day's allowance is used up"
    assert tomorrow is True, "a new day must start empty without any scheduled reset"


# ── the lease sweep (FR-10.5) ───────────────────────────────────────────


async def test_a_worker_that_died_is_noticed_by_the_sweep(clean, settings) -> None:
    await _register(1)
    async with db.session() as session:
        await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
        claimed = await queue.claim(session, settings)
        assert claimed is not None
        job_id = claimed.id
        # The worker stopped renewing, so the lease simply ran out.
        claimed.lease_expires = NOW - timedelta(seconds=1)

    reclaimed = await scheduler.sweep_leases(settings, now=NOW)

    assert reclaimed.requeued == (job_id,)
    async with db.session() as session:
        assert (await session.get(Job, job_id)).status == queue.QUEUED


async def test_a_live_lease_is_left_alone(clean, settings) -> None:
    await _register(1)
    async with db.session() as session:
        await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
        claimed = await queue.claim(session, settings)
        assert claimed is not None

    reclaimed = await scheduler.sweep_leases(settings, now=NOW)

    assert reclaimed.total == 0


# ── retention (D21) ─────────────────────────────────────────────────────


async def test_history_older_than_the_window_is_pruned(clean, settings) -> None:
    kept = _with_settings(settings, retention_days=7)
    await _register(1)
    old = await _finish_a_job(1, settings, status=queue.DELIVERED, at=NOW - timedelta(days=9))
    recent = await _finish_a_job(1, settings, status=queue.FAILED, at=NOW - timedelta(days=1))

    pruned = await scheduler.prune_history(kept, now=NOW)

    assert pruned == (old,)
    async with db.session() as session:
        assert await session.get(Job, old) is None
        assert await session.get(Job, recent) is not None


async def test_active_work_is_never_pruned(clean, settings) -> None:
    """A job with no ``finished_at`` cannot match, however old the queue is."""
    kept = _with_settings(settings, retention_days=0)
    await _register(1)
    async with db.session() as session:
        job = await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
        claimed = await queue.claim(session, settings)
        assert claimed is not None
        # Old enough to look stale, but still running.
        claimed.created_at = NOW - timedelta(days=400)

    pruned = await scheduler.prune_history(kept, now=NOW)

    assert pruned == ()
    async with db.session() as session:
        assert await session.get(Job, job.id) is not None


async def test_pruning_removes_the_staging_directory_too(
    clean, settings, tmp_path, monkeypatch
) -> None:
    """Otherwise the database stays tidy while the disk fills up."""
    monkeypatch.setattr(paths, "TMP_DIR", tmp_path)
    kept = _with_settings(settings, retention_days=1)
    await _register(1)
    job_id = await _finish_a_job(1, settings, status=queue.DELIVERED, at=NOW - timedelta(days=5))
    staging = tmp_path / str(job_id)
    staging.mkdir()
    (staging / "video.mp4").write_bytes(b"x")

    await scheduler.prune_history(kept, now=NOW)

    assert not staging.exists()


# ── orphans left by a hard kill (D21) ───────────────────────────────────


async def test_an_orphaned_staging_directory_is_swept(
    clean, settings, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(paths, "TMP_DIR", tmp_path)
    await _register(1)
    live = await _finish_a_job(1, settings, status=queue.DELIVERED, at=NOW - timedelta(days=1))
    (tmp_path / str(live)).mkdir()
    orphan = tmp_path / "999999"
    orphan.mkdir()

    removed = await scheduler.sweep_orphans()

    assert removed == (orphan,)
    assert not orphan.exists()
    assert (tmp_path / str(live)).exists(), "a directory with a live job must survive"


async def test_an_absent_staging_root_is_not_an_error(
    clean, settings, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(paths, "TMP_DIR", tmp_path / "never-created")

    assert await scheduler.orphaned_temp_dirs() == ()


# ── the loop (D18) ──────────────────────────────────────────────────────


async def test_one_pass_does_both_jobs(clean, settings) -> None:
    kept = _with_settings(settings, retention_days=1)
    # Two users: the first one's only free-tier slot is held by the abandoned job below, so a
    # second job for the same user could not be claimed.
    await _register(1, 2)
    async with db.session() as session:
        await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
        claimed = await queue.claim(session, kept)
        assert claimed is not None
        claimed.lease_expires = NOW - timedelta(seconds=1)
    old = await _finish_a_job(2, kept, status=queue.DELIVERED, at=NOW - timedelta(days=3))

    result = await scheduler.housekeeping(kept, now=NOW)

    assert result.reclaimed.total == 1
    assert result.pruned_jobs == (old,)


async def test_the_loop_recovers_abandoned_work_on_its_own(clean, settings) -> None:
    await _register(1)
    async with db.session() as session:
        await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
        claimed = await queue.claim(session, settings)
        assert claimed is not None
        job_id = claimed.id
        claimed.lease_expires = datetime.now(UTC) - timedelta(seconds=1)

    loop = scheduler.Scheduler(settings, sweep_interval=0.05)
    await loop.start()
    try:
        for _ in range(100):
            async with db.session() as session:
                if (await session.get(Job, job_id)).status == queue.QUEUED:
                    break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the sweep never ran")
    finally:
        await loop.stop()

    assert loop.running is False


async def test_retention_only_runs_when_it_is_due(clean, settings) -> None:
    """The clock is injected, so "due" can be decided without waiting an hour (D18)."""
    kept = _with_settings(settings, retention_days=1)
    await _register(1)
    old = await _finish_a_job(1, kept, status=queue.DELIVERED, at=NOW - timedelta(days=3))

    clock = [NOW]
    loop = scheduler.Scheduler(
        kept, sweep_interval=1.0, retention_interval=3600.0, clock=lambda: clock[0]
    )

    first = await loop.tick()
    assert first.pruned_jobs == (old,), "the first pass always considers retention"

    recent = await _finish_a_job(1, kept, status=queue.FAILED, at=clock[0] - timedelta(days=2))
    clock[0] += timedelta(minutes=5)
    second = await loop.tick()

    assert second.pruned_jobs == (), "an hour has not passed, so retention is not due"
    async with db.session() as session:
        assert await session.get(Job, recent) is not None
