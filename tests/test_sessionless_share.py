"""Sessionless worker share: a hard cap on jobs without a live session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import queue, sessions
from core.metrics import get_metrics


def _reset_queue() -> None:
    queue._jobs.clear()  # noqa: SLF001 - the queue is process-local state


@pytest.fixture
def share(monkeypatch: pytest.MonkeyPatch):
    """Cap sessionless jobs at 5 and treat every user as sessionless."""
    online: set[int] = set()
    settings = SimpleNamespace(
        sessionless_worker_slots=5, job_max_attempts=3, max_concurrent_downloads=20
    )

    monkeypatch.setattr(queue, "get_settings", lambda: settings)
    monkeypatch.setattr(sessions, "is_client_active", lambda user_id: user_id in online)
    _reset_queue()
    yield online, settings
    _reset_queue()


async def _enqueue_many(count: int, *, first_user: int = 1) -> list[queue.Job]:
    jobs = []
    for offset in range(count):
        jobs.append(
            await queue.enqueue(
                user_id=first_user + offset,
                link=f"https://example.com/{offset}",
                tier="free",
            )
        )
    return jobs


async def test_five_of_six_sessionless_jobs_are_claimed(share) -> None:
    jobs = await _enqueue_many(6)

    claimed = [await queue.claim_next() for _ in range(5)]

    assert claimed == jobs[:5]
    assert all(job is not None and job.sessionless for job in claimed)
    assert await queue.claim_next() is None  # the sixth stays queued
    assert jobs[5].status == queue.QUEUED
    assert get_metrics().snapshot()["sessionless_active"] == 5


async def test_finishing_a_sessionless_job_frees_a_slot(share) -> None:
    jobs = await _enqueue_many(6)
    for _ in range(5):
        await queue.claim_next()

    await queue.complete(jobs[0], bytes_downloaded=1)

    assert await queue.claim_next() is jobs[5]


async def test_sessioned_job_is_claimed_while_the_share_is_full(share) -> None:
    online, _settings = share
    await _enqueue_many(5)
    for _ in range(5):
        await queue.claim_next()

    online.add(99)  # this user has a live session
    sessioned = await queue.enqueue(user_id=99, link="https://example.com/live", tier="free")
    sixth = await queue.enqueue(user_id=6, link="https://example.com/sixth", tier="free")

    claimed = await queue.claim_next()

    assert claimed is not None
    assert claimed is sessioned
    assert claimed.sessionless is False
    assert sixth.status == queue.QUEUED


async def test_admin_jobs_bypass_the_share(share) -> None:
    online, settings = share
    settings.sessionless_worker_slots = 1
    online.clear()

    await _enqueue_many(1)
    assert await queue.claim_next() is not None

    admin_job = await queue.enqueue(
        user_id=77, link="https://example.com/admin", tier="premium", is_admin=True
    )

    assert await queue.claim_next() is admin_job
    assert admin_job.sessionless is False


async def test_zero_disables_the_share(share) -> None:
    _online, settings = share
    settings.sessionless_worker_slots = 0

    jobs = await _enqueue_many(6)

    claimed = [await queue.claim_next() for _ in range(6)]

    assert claimed == jobs


async def test_deferred_claims_are_counted(share) -> None:
    await _enqueue_many(7)
    for _ in range(5):
        await queue.claim_next()
    before = get_metrics().snapshot()["sessionless_deferred"]

    assert await queue.claim_next() is None

    assert get_metrics().snapshot()["sessionless_deferred"] > before
