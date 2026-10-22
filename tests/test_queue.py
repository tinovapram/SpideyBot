from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core import queue, sessions


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


# ── ineligible jobs must not spin the worker loop ────────────────


def _share_settings(sessionless_slots: int = 5):
    return SimpleNamespace(
        sessionless_worker_slots=sessionless_slots,
        job_max_attempts=3,
        max_concurrent_downloads=20,
    )


@pytest.fixture
def sessionless_env(monkeypatch: pytest.MonkeyPatch):
    online: set[int] = set()
    monkeypatch.setattr(
        queue, "get_settings", lambda: _share_settings(sessionless_slots=1)
    )
    monkeypatch.setattr(sessions, "is_client_active", lambda user_id: user_id in online)
    queue._jobs.clear()
    yield online
    queue._jobs.clear()


async def _elapsed_waiting(timeout: float) -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    await queue.wait_for_work(timeout=timeout)
    return loop.time() - started


@pytest.mark.asyncio
async def test_wait_for_work_blocks_when_the_sessionless_share_is_full(
    sessionless_env,
) -> None:
    first = await queue.enqueue(user_id=1, link="https://example.com/a", tier="free")
    second = await queue.enqueue(user_id=2, link="https://example.com/b", tier="free")

    assert await queue.claim_next() is first  # fills the only sessionless slot

    # `second` is queued but cannot be claimed: waiting must be real, not a spin.
    assert await _elapsed_waiting(0.15) >= 0.12

    await queue.complete(first)  # freeing a slot notifies the waiters
    assert await _elapsed_waiting(0.15) < 0.1
    assert await queue.claim_next() is second


@pytest.mark.asyncio
async def test_wait_for_work_blocks_when_a_users_slots_are_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queue, "get_settings", lambda: _share_settings())
    await queue.clear()

    first = await queue.enqueue(user_id=7, link="https://example.com/1", tier="free")
    await queue.enqueue(user_id=7, link="https://example.com/2", tier="free")

    assert await queue.claim_next() is first
    assert await _elapsed_waiting(0.15) >= 0.12

    await queue.complete(first)
    assert await _elapsed_waiting(0.15) < 0.1

    await queue.clear()


@pytest.mark.asyncio
async def test_wait_for_work_reports_whether_it_waited() -> None:
    monkeypatch_settings = _share_settings()
    original = queue.get_settings
    queue.get_settings = lambda: monkeypatch_settings  # type: ignore[assignment]
    try:
        await queue.clear()
        await queue.enqueue(user_id=8, link="https://example.com/ready", tier="free")

        # Claimable work: return at once and tell the caller it did not wait.
        assert await queue.wait_for_work(timeout=1.0) is False
    finally:
        queue.get_settings = original  # type: ignore[assignment]
        await queue.clear()
