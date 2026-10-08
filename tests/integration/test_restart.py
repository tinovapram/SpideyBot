"""Surviving a restart: in-flight work resumes, finished work never re-runs (§8.2, E26).

These are the guarantees that only exist because the queue is a database table rather than a
list. A restart is simulated the way one actually happens — the process stops renewing a lease
and writes nothing more — because that, and not a tidy shutdown, is the case that loses or
duplicates files.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app import db, queue, scheduler, users, workers
from app.models import Job

LINK = "https://example.test/album/1"


async def _register(*user_ids: int) -> None:
    async with db.session() as session:
        for user_id in user_ids:
            await users.upsert(session, user_id=user_id, first_name="T")


async def _submit(user_id: int) -> int:
    async with db.session() as session:
        job = await queue.enqueue(session, user_id=user_id, link=LINK, tier="free", priority=2.0)
        return job.id


async def _claim_and_die(settings) -> int:
    """Claim a job, then abandon it the way a killed process does.

    No terminal state is written and nothing renews the lease, so the only trace left is a
    running row whose lease has expired.
    """
    async with db.session() as session:
        claimed = await queue.claim(session, settings)
        assert claimed is not None
        claimed.lease_expires = datetime.now(UTC) - timedelta(seconds=1)
        return claimed.id


async def _job(job_id: int) -> Job:
    async with db.session() as session:
        return await session.get(Job, job_id)


def _collector(ran: list[int]):
    async def handler(job, keeper):
        ran.append(job.id)
        return workers.JobOutcome(ok=True)

    return handler


async def _drain(pool: workers.WorkerPool, *, seconds: float = 15.0) -> None:
    await pool.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await pool.stop()


async def test_work_that_was_in_flight_is_resumed_after_a_restart(clean, settings) -> None:
    """The job the dead process left behind is recovered, run once, and delivered."""
    await _register(1)
    job_id = await _submit(1)
    await _claim_and_die(settings)

    # Restart. The sweep is what notices: nothing else can tell this apart from a slow job.
    recovered = await scheduler.sweep_leases(settings)

    assert recovered.requeued == (job_id,)

    ran: list[int] = []
    pool = workers.WorkerPool(settings, _collector(ran), idle_sleep=0.05, size=1)
    await pool.start()
    try:
        for _ in range(200):
            if ran:
                break
            await asyncio.sleep(0.05)
    finally:
        await pool.stop()

    assert ran == [job_id], "the recovered job ran exactly once"

    job = await _job(job_id)
    assert job.status == queue.DELIVERED
    assert job.attempts == 2, (
        "the attempt lost to the crash still counts, so a poison link cannot loop"
    )


async def test_a_delivered_job_is_never_run_again(clean, settings) -> None:
    """E26's other half: replaying it would re-send files the user already has."""
    await _register(1)
    job_id = await _submit(1)

    async def deliver(job, keeper):
        return workers.JobOutcome(ok=True)

    await workers.run_once(settings, deliver)
    delivered = await _job(job_id)
    assert delivered.status == queue.DELIVERED
    delivered_at = delivered.finished_at

    # Restart, complete with a sweep that would requeue anything it found running.
    await scheduler.sweep_leases(settings)
    ran: list[int] = []
    await _drain(
        workers.WorkerPool(settings, _collector(ran), idle_sleep=0.05, size=1), seconds=1.0
    )

    assert ran == [], "a finished job must not be picked up by the new process"

    job = await _job(job_id)
    assert job.status == queue.DELIVERED
    assert job.finished_at == delivered_at, "and its result must not be overwritten"


async def test_a_restart_does_not_duplicate_a_job_that_finished_mid_flight(clean, settings) -> None:
    """A job that delivered *before* the crash is not re-run when the sweep looks at it.

    This is the case a naive "requeue everything that was running" recovery gets wrong: the row
    may have been written as delivered moments before the process died.
    """
    await _register(1)
    job_id = await _submit(1)

    async def deliver(job, keeper):
        return workers.JobOutcome(ok=True)

    await workers.run_once(settings, deliver)
    job = await _job(job_id)
    assert job.status == queue.DELIVERED
    # The crash happened after delivery but before anything else could touch the row, so the
    # only thing left is to check that recovery leaves it alone.
    job.lease_expires = datetime.now(UTC) - timedelta(seconds=1)

    recovered = await scheduler.sweep_leases(settings)

    assert recovered.total == 0
    assert (await _job(job_id)).status == queue.DELIVERED


async def test_cancelled_work_is_not_resurrected_by_a_restart(clean, settings) -> None:
    """A user who cancelled before the crash should not get the download anyway."""
    await _register(1)
    job_id = await _submit(1)
    await _claim_and_die(settings)
    async with db.session() as session:
        await queue.request_cancel(session, job_id=job_id, user_id=1)

    recovered = await scheduler.sweep_leases(settings)

    assert recovered.cancelled == (job_id,)
    assert (await _job(job_id)).status == queue.CANCELLED

    ran: list[int] = []
    await _drain(
        workers.WorkerPool(settings, _collector(ran), idle_sleep=0.05, size=1), seconds=1.0
    )

    assert ran == [], "a cancelled job must stay cancelled across a restart"
