from __future__ import annotations

import pytest

from core import queue


@pytest.mark.asyncio
async def test_process_local_queue_claims_and_clears_jobs() -> None:
    await queue.clear()

    job = await queue.enqueue(
        None,
        user_id=42,
        link="https://example.com/video",
        site="example",
        tier="free",
    )

    claimed = await queue.claim_next(None)
    assert claimed is job
    assert claimed.status == queue.RUNNING

    await queue.clear()
    assert await queue.active_count(user_id=42) == 0


@pytest.mark.asyncio
async def test_queue_enforces_per_user_concurrency_limit() -> None:
    await queue.clear()

    first = await queue.enqueue(None, user_id=42, link="https://example.com/1", tier="free")
    second = await queue.enqueue(None, user_id=42, link="https://example.com/2", tier="free")

    assert await queue.claim_next(None) is first
    assert await queue.claim_next(None) is None

    await queue.complete(None, first, bytes_downloaded=1)
    assert await queue.claim_next(None) is second

    await queue.clear()
