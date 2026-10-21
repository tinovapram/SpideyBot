from __future__ import annotations

import asyncio

import pytest

from core import queue


@pytest.mark.asyncio
async def test_process_local_queue_claims_and_clears_jobs() -> None:
    await queue.clear()

    job = await queue.enqueue(
        user_id=42,
        link="https://example.com/video",
        site="example",
        tier="free",
    )

    claimed = await queue.claim_next()
    assert claimed is job
    assert claimed.status == queue.RUNNING

    await queue.clear()
    assert await queue.active_count(user_id=42) == 0


@pytest.mark.asyncio
async def test_queue_enforces_per_user_concurrency_limit() -> None:
    await queue.clear()

    first = await queue.enqueue(user_id=42, link="https://example.com/1", tier="free")
    second = await queue.enqueue(user_id=42, link="https://example.com/2", tier="free")

    assert await queue.claim_next() is first
    assert await queue.claim_next() is None

    await queue.complete(first, bytes_downloaded=1)
    assert await queue.claim_next() is second

    await queue.clear()


@pytest.mark.asyncio
async def test_finished_jobs_are_discarded_from_the_process_queue() -> None:
    await queue.clear()

    completed = await queue.enqueue(user_id=1, link="https://example.com/a", tier="free")
    await queue.claim_next()
    await queue.complete(completed)
    assert completed.id not in queue._jobs

    failed = await queue.enqueue(user_id=2, link="https://example.com/b", tier="free")
    await queue.claim_next()
    await queue.fail(failed, retry=False)
    assert failed.id not in queue._jobs

    cancelled = await queue.enqueue(user_id=3, link="https://example.com/c", tier="free")
    assert await queue.cancel(cancelled.id) is True
    assert cancelled.id not in queue._jobs


@pytest.mark.asyncio
async def test_wait_for_work_wakes_up_on_enqueue() -> None:
    await queue.clear()

    async def enqueue_soon() -> None:
        await asyncio.sleep(0.05)
        await queue.enqueue(user_id=7, link="https://example.com/w", tier="free")

    task = asyncio.create_task(enqueue_soon())
    loop = asyncio.get_running_loop()
    started = loop.time()
    await queue.wait_for_work(timeout=5.0)
    elapsed = loop.time() - started
    await task

    assert elapsed < 1.0
    assert await queue.claim_next() is not None
    await queue.clear()


@pytest.mark.asyncio
async def test_queue_position_follows_claim_order() -> None:
    await queue.clear()

    standard = await queue.enqueue(user_id=1, link="https://example.com/s", tier="free")
    priority = await queue.enqueue(user_id=2, link="https://example.com/p", tier="premium")

    # premium (priority 0.5) is claimed before free (priority 2.0)
    assert await queue.position(priority.id) == 1
    assert await queue.position(standard.id) == 2

    await queue.clear()
