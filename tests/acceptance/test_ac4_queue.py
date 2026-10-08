"""AC-4: a second link waits for a slot, reports its place, and starts by itself.

Full path, with the real pool: submit twice, watch the second wait while the first runs, then
release the first and confirm nobody had to touch anything for the second to start. The slot
limit is the free tier's, so this is also where an oversell would show up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app import db, queue, users, workers
from app.models import Job

LINK = "https://example.test/album/1"


async def _register(*user_ids: int) -> None:
    async with db.session() as session:
        for user_id in user_ids:
            await users.upsert(session, user_id=user_id, first_name="T")


async def _submit(session, user_id: int) -> Job:
    return await queue.enqueue(session, user_id=user_id, link=LINK, tier="free", priority=2.0)


async def _status(job_id: int) -> str:
    async with db.session() as session:
        return (await session.get(Job, job_id)).status


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was never met")


async def test_a_free_user_gets_one_slot_and_the_rest_queue(clean, settings) -> None:
    """The other half of FR-10.1, seen from the user's side."""
    await _register(1)
    async with db.session() as session:
        first = await _submit(session, 1)
        second = await _submit(session, 1)

    async with db.session() as session:
        assert await queue.queue_position(session, second) == 2, "second in line behind the first"

    async with db.session() as session:
        claimed = await queue.claim(session, settings)
        assert claimed is not None and claimed.id == first.id

    assert await _status(second.id) == queue.QUEUED
    async with db.session() as session:
        assert await queue.queue_position(session, second) == 1, "now at the front of the line"


async def test_the_waiting_job_starts_by_itself_when_the_first_finishes(clean, settings) -> None:
    await _register(1)
    async with db.session() as session:
        first = await _submit(session, 1)
        second = await _submit(session, 1)

    ran: list[int] = []
    first_released = asyncio.Event()

    async def handler(job, keeper):
        ran.append(job.id)
        if job.id == first.id:
            # Holds the only free slot until the test says otherwise.
            await first_released.wait()
        return workers.JobOutcome(ok=True)

    pool = workers.WorkerPool(settings, handler, idle_sleep=0.05, size=1)
    await pool.start()
    try:
        await _wait_for(lambda: first.id in ran)
        assert await _status(first.id) == queue.RUNNING
        assert await _status(second.id) == queue.QUEUED, "the slot is not oversold"

        first_released.set()

        # No second submission, no second claim: the pool picks the waiting job up on its own.
        await _wait_for(lambda: second.id in ran)
    finally:
        await pool.stop()

    assert await _status(first.id) == queue.DELIVERED
    assert await _status(second.id) == queue.DELIVERED


async def test_a_second_user_is_not_blocked_by_the_first(clean, settings) -> None:
    """FR-10.2: the queue is per tier, so one busy user does not stall everyone else."""
    await _register(1, 2)
    async with db.session() as session:
        holder = await _submit(session, 1)
        waiting = await _submit(session, 1)
        other = await _submit(session, 2)

    async with db.session() as session:
        first = await queue.claim(session, settings)
        second = await queue.claim(session, settings)

    assert first is not None and first.id == holder.id
    assert second is not None and second.id == other.id, "user 2 runs while user 1 is full"
    assert await _status(waiting.id) == queue.QUEUED
