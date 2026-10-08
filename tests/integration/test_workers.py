"""Worker slots: running a claimed job, keeping its lease, and recording the outcome.

These drive real jobs through a real Postgres, because the interesting behaviour is what
happens *while* a job runs — the lease being renewed, a cancellation arriving, another worker
taking the job over — and none of that exists without a second connection to observe it.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select, update

from app import db, queue, users, workers
from app.config import Settings
from app.models import Job

LINK = "https://example.test/album/1"


def _with_runtime(settings: Settings, **overrides: int) -> Settings:
    return settings.model_copy(update={"runtime": settings.runtime.model_copy(update=overrides)})


async def _register(*user_ids: int) -> None:
    async with db.session() as session:
        for user_id in user_ids:
            await users.upsert(session, user_id=user_id, username=f"u{user_id}", first_name="T")


async def _queue_job(user_id: int, *, tier: str = "free") -> int:
    async with db.session() as session:
        job = await queue.enqueue(session, user_id=user_id, link=LINK, tier=tier, priority=2.0)
        return job.id


async def _status_of(job_id: int) -> str:
    async with db.session() as session:
        return (await session.get(Job, job_id)).status


# ── one job at a time ───────────────────────────────────────────────────


async def test_an_empty_queue_leaves_the_worker_idle(clean, settings) -> None:
    async def handler(job, keeper):  # pragma: no cover - must never run
        raise AssertionError("nothing was queued")

    assert await workers.run_once(settings, handler) is None


async def test_a_successful_run_delivers_the_job(clean, settings) -> None:
    await _register(1)
    job_id = await _queue_job(1)

    async def handler(job, keeper):
        return workers.JobOutcome(ok=True)

    job = await workers.run_once(settings, handler)

    assert job is not None and job.id == job_id
    assert job.status == queue.DELIVERED
    assert job.finished_at is not None


async def test_a_failure_with_attempts_left_goes_back_to_the_queue(clean, settings) -> None:
    await _register(1)
    job_id = await _queue_job(1)

    async def handler(job, keeper):
        return workers.failure("the host stopped responding")

    job = await workers.run_once(settings, handler)

    assert job is not None
    assert job.status == queue.QUEUED
    assert job.claim_token is None
    assert await _status_of(job_id) == queue.QUEUED


async def test_a_failure_that_used_up_its_attempts_is_terminal(clean, settings) -> None:
    tight = _with_runtime(settings, job_max_attempts=1)
    await _register(1)
    job_id = await _queue_job(1)

    async def handler(job, keeper):
        return workers.failure("still broken")

    job = await workers.run_once(tight, handler)

    assert job is not None
    assert job.status == queue.FAILED
    assert await _status_of(job_id) == queue.FAILED


async def test_a_handler_that_raises_does_not_take_the_worker_down(clean, settings) -> None:
    """One bad link must not stop every other user's downloads."""
    await _register(1)
    job_id = await _queue_job(1)

    async def handler(job, keeper):
        raise RuntimeError("unexpected")

    job = await workers.run_once(settings, handler)

    assert job is not None
    # Requeued rather than failed: an unexpected error is treated as a failed attempt.
    assert job.status == queue.QUEUED
    assert await _status_of(job_id) == queue.QUEUED


# ── cancellation during a run (FR-11.5, FR-11.6) ────────────────────────


async def test_a_cancellation_during_a_run_closes_the_job(clean, settings) -> None:
    """The handler has to be told, because a database write cannot interrupt a transfer."""
    quick = _with_runtime(settings, job_lease_seconds=3)
    await _register(1)
    job_id = await _queue_job(1)

    started = asyncio.Event()
    noticed = asyncio.Event()

    async def handler(job, keeper):
        started.set()
        for _ in range(200):
            if keeper.stop_requested:
                noticed.set()
                return workers.failure("cancelled by the user")
            await asyncio.sleep(0.05)
        return workers.JobOutcome(ok=True)

    running = asyncio.create_task(workers.run_once(quick, handler))
    await started.wait()
    async with db.session() as session:
        await queue.request_cancel(session, job_id=job_id, user_id=1)

    job = await asyncio.wait_for(running, timeout=30)

    assert noticed.is_set(), "the keeper never surfaced the cancellation to the handler"
    assert job is not None
    assert job.status == queue.CANCELLED
    assert await _status_of(job_id) == queue.CANCELLED


async def test_a_run_that_outlives_a_cancellation_is_still_cancelled(clean, settings) -> None:
    """Even a run that delivered as the request arrived is recorded as the user asked.

    This is the case the handler cannot see: it finished before it ever polled.
    """
    quick = _with_runtime(settings, job_lease_seconds=3)
    await _register(1)
    job_id = await _queue_job(1)

    started = asyncio.Event()

    async def handler(job, keeper):
        started.set()
        await asyncio.sleep(1.2)  # never looks at keeper.stop_requested
        return workers.JobOutcome(ok=True)

    running = asyncio.create_task(workers.run_once(quick, handler))
    await started.wait()
    async with db.session() as session:
        await queue.request_cancel(session, job_id=job_id, user_id=1)

    job = await asyncio.wait_for(running, timeout=30)

    assert job is not None
    assert job.status == queue.CANCELLED
    assert job.finished_at is not None


# ── leases during a run (FR-10.5, D15) ──────────────────────────────────


async def test_the_lease_is_kept_alive_while_a_job_runs(clean, settings) -> None:
    """A job that takes longer than its lease must not be handed to a second worker."""
    quick = _with_runtime(settings, job_lease_seconds=3)
    await _register(1)
    await _queue_job(1)

    observed: list[tuple[object, object]] = []

    async def handler(job, keeper):
        before = job.lease_expires
        await asyncio.sleep(1.6)  # at least one renewal interval
        async with db.session() as session:
            row = await session.get(Job, job.id)
            observed.append((before, row.lease_expires))
        return workers.JobOutcome(ok=True)

    await workers.run_once(quick, handler)

    assert observed, "the handler never sampled the lease"
    before, after = observed[0]
    assert after > before, "the lease was allowed to run down instead of being renewed"


async def test_a_run_whose_lease_was_taken_over_changes_nothing(clean, settings) -> None:
    """The other worker owns the job now, so this run's outcome is stale and must be dropped.

    Otherwise a worker that was presumed dead would come back and overwrite the real result.
    """
    quick = _with_runtime(settings, job_lease_seconds=3)
    await _register(1)
    job_id = await _queue_job(1)

    async def handler(job, keeper):
        await asyncio.sleep(1.6)  # let the keeper try to renew; it must fail
        return workers.JobOutcome(ok=True)

    async def steal():
        await asyncio.sleep(0.5)
        async with db.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(claim_token=str(uuid.uuid4()))
                .execution_options(synchronize_session=False)
            )

    await asyncio.gather(workers.run_once(quick, handler), steal())

    # Still running, still owned by the thief: no terminal state was written.
    assert await _status_of(job_id) == queue.RUNNING


# ── the pool ────────────────────────────────────────────────────────────


async def _drain(pool: workers.WorkerPool, expected: int, timeout: float = 20.0) -> None:
    """Run the pool until it has delivered ``expected`` jobs, then stop it."""
    await pool.start()
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await _delivered_count() >= expected:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"only {await _delivered_count()} of {expected} jobs were delivered")
    finally:
        await pool.stop()


async def _delivered_count() -> int:
    async with db.session() as session:
        result = await session.execute(select(Job.id).where(Job.status == queue.DELIVERED))
        return len(result.scalars().all())


async def test_the_pool_drains_the_queue(clean, settings) -> None:
    pool_settings = _with_runtime(settings, max_concurrent_downloads=2)
    await _register(1, 2, 3)
    job_ids = [await _queue_job(user_id) for user_id in (1, 2, 3)]

    ran: list[int] = []

    async def handler(job, keeper):
        ran.append(job.id)
        return workers.JobOutcome(ok=True)

    pool = workers.WorkerPool(pool_settings, handler, idle_sleep=0.05)
    assert pool.size == 2

    await _drain(pool, expected=len(job_ids))

    assert sorted(ran) == sorted(job_ids)
    async with db.session() as session:
        statuses = set((await session.execute(select(Job.status))).scalars().all())
    assert statuses == {queue.DELIVERED}


async def test_a_slot_survives_a_job_that_fails(clean, settings) -> None:
    """The slot must come back for the next job: a poison link cannot end the pool."""
    pool_settings = _with_runtime(settings, max_concurrent_downloads=1, job_max_attempts=1)
    await _register(1, 2)
    poison = await _queue_job(1)
    good = await _queue_job(2)

    async def handler(job, keeper):
        if job.id == poison:
            raise RuntimeError("poison link")
        return workers.JobOutcome(ok=True)

    pool = workers.WorkerPool(pool_settings, handler, idle_sleep=0.05)
    await _drain(pool, expected=1)

    assert await _status_of(poison) == queue.FAILED
    assert await _status_of(good) == queue.DELIVERED
