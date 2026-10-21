"""Process-local priority queue for active downloads.

Jobs intentionally disappear when the process stops. User accounts, quotas,
cookies, and Telegram session files remain persistent elsewhere.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.config import get_settings
from core.metrics import get_metrics
from core.tiers import PRIORITY_ADMIN, tier_policy

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

_ids = itertools.count(1)
_jobs: dict[int, Job] = {}
_lock = asyncio.Lock()
_work_available = asyncio.Event()


def notify_work() -> None:
    """Wake up an idle worker: a job may now be claimable."""
    _work_available.set()


async def wait_for_work(timeout: float = 1.0) -> None:
    """Block until a job may be claimable, or *timeout* elapses.

    Clearing before the check keeps an ``enqueue()`` that lands in between from
    being lost.
    """
    _work_available.clear()
    if any(job.status == QUEUED for job in _jobs.values()):
        return
    try:
        await asyncio.wait_for(_work_available.wait(), timeout)
    except TimeoutError:
        pass


async def position(job_id: int) -> int:
    """1-based rank of a queued job in claim order, or 0 when it isn't queued."""
    async with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != QUEUED:
            return 0
        queued = sorted(
            (item for item in _jobs.values() if item.status == QUEUED),
            key=lambda item: (item.priority, item.created_at, item.id),
        )
        for index, candidate in enumerate(queued, 1):
            if candidate.id == job_id:
                return index
        return 0


@dataclass
class Job:
    """In-memory download job. It is never written to the database."""

    id: int
    user_id: int
    link: str
    site: str | None
    tier: str
    priority: float
    status: str = QUEUED
    attempts: int = 0
    total_bytes: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None


async def enqueue(
    _session: Any,
    *,
    user_id: int,
    link: str,
    site: str | None = None,
    tier: str = "free",
    is_admin: bool = False,
) -> Job:
    """Add a job to this process's queue."""
    job = Job(
        id=next(_ids),
        user_id=user_id,
        link=link,
        site=site,
        tier=tier,
        priority=PRIORITY_ADMIN if is_admin else tier_policy(tier).priority,
    )
    async with _lock:
        _jobs[job.id] = job
    get_metrics().incr("tasks_enqueued")
    await _update_gauges()
    notify_work()
    return job


async def claim_next(_session: Any) -> Job | None:
    """Claim the highest-priority queued job in this process."""
    async with _lock:
        active_by_user: dict[int, int] = {}
        for running_job in _jobs.values():
            if running_job.status == RUNNING:
                active_by_user[running_job.user_id] = active_by_user.get(running_job.user_id, 0) + 1

        candidates = [
            job
            for job in _jobs.values()
            if (
                job.status == QUEUED
                and active_by_user.get(job.user_id, 0) < tier_policy(job.tier).concurrent
            )
        ]
        if not candidates:
            return None
        job = min(candidates, key=lambda item: (item.priority, item.created_at, item.id))
        job.status = RUNNING
        job.attempts += 1
        job.started_at = job.started_at or datetime.now(UTC)
    get_metrics().incr("queue_claims")
    await _update_gauges()
    return job


async def _update_gauges(discard: int | None = None) -> None:
    async with _lock:
        if discard is not None:
            # Finished jobs must not accumulate: the dict would otherwise grow
            # for the whole process lifetime.
            _jobs.pop(discard, None)
        metrics = get_metrics()
        metrics.set("queue_depth", sum(job.status == QUEUED for job in _jobs.values()))
        metrics.set("active_downloads", sum(job.status == RUNNING for job in _jobs.values()))


async def active_count(user_id: int) -> int:
    async with _lock:
        return sum(
            job.user_id == user_id and job.status == RUNNING
            for job in _jobs.values()
        )


async def complete(
    _session: Any,
    job: Job,
    *,
    bytes_downloaded: int | None = None,
    filename: str | None = None,
) -> None:
    """Mark an in-memory job complete."""
    job.status = DONE
    job.finished_at = datetime.now(UTC)
    job.total_bytes = bytes_downloaded
    get_metrics().incr("downloads_completed")
    get_metrics().record_download(
        job.site,
        (job.finished_at - (job.started_at or job.created_at)).total_seconds(),
    )
    if bytes_downloaded:
        get_metrics().incr("bytes_downloaded", bytes_downloaded)
    await _update_gauges(discard=job.id)
    notify_work()  # a concurrency slot just freed up


async def fail(_session: Any, job: Job, *, retry: bool = True) -> bool:
    """Fail a job or put it back in the process-local queue."""
    settings = get_settings()
    job.finished_at = datetime.now(UTC)
    if retry and job.attempts < settings.job_max_attempts:
        job.status = QUEUED
        get_metrics().incr("downloads_failed")
        get_metrics().record_failure(job.site)
        await _update_gauges()
        notify_work()
        return True
    job.status = FAILED
    get_metrics().incr("downloads_failed")
    get_metrics().record_failure(job.site)
    await _update_gauges(discard=job.id)
    return False


async def retry_running_job(_session: Any, job: Job) -> bool:
    """Retry a failed job without making it visible outside this process."""
    settings = get_settings()
    get_metrics().incr("downloads_failed")
    get_metrics().incr("download_retries")
    if job.attempts >= settings.job_max_attempts:
        job.status = FAILED
        job.finished_at = datetime.now(UTC)
        await _update_gauges(discard=job.id)
        return False
    job.attempts += 1
    job.status = RUNNING
    job.finished_at = None
    return True


async def cancel(_session: Any, job_id: int) -> bool:
    async with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != QUEUED:
            return False
        job.status = CANCELLED
        job.finished_at = datetime.now(UTC)
    get_metrics().incr("downloads_cancelled")
    await _update_gauges(discard=job_id)
    notify_work()
    return True


async def clear() -> None:
    """Clear all process-local jobs, primarily for tests and shutdown."""
    async with _lock:
        _jobs.clear()
    await _update_gauges()
