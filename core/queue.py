"""Persistent priority download queue backed by PostgreSQL.

Workers claim jobs atomically with ``FOR UPDATE SKIP LOCKED``.  Each claim
sets ``status='running'``, a fresh ``claim_token``, and ``lease_expires``.
If a worker crashes, the scheduler re-queues expired leases.

Horizontal-scale ready: multiple worker processes can share the same queue
with no code changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.metrics import get_metrics
from core.models import DownloadJob, DownloadHistory
from core.tiers import PRIORITY_ADMIN, tier_policy

# ── Status constants ────────────────────────────────────────────

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


# ── Enqueue ────────────────────────────────────────────────────


async def enqueue(
    session: AsyncSession,
    *,
    user_id: int,
    link: str,
    site: str | None = None,
    tier: str = "free",
    is_admin: bool = False,
) -> DownloadJob:
    """Insert a new job and return it."""
    priority = PRIORITY_ADMIN if is_admin else tier_policy(tier).priority
    job = DownloadJob(
        user_id=user_id,
        link=link,
        site=site,
        tier=tier,
        priority=priority,
        status=QUEUED,
        attempts=0,
    )
    session.add(job)
    await session.flush()
    return job


# ── Claim (worker) ─────────────────────────────────────────────


async def claim_next(session: AsyncSession) -> DownloadJob | None:
    """Atomically claim the next runnable job.

    Uses ``FOR UPDATE SKIP LOCKED`` so multiple workers never grab the same
    job.  Returns ``None`` when the queue is empty.
    """
    settings = get_settings()
    lease_seconds = settings.job_lease_seconds

    stmt = text(
        """
        UPDATE download_jobs SET
            status        = 'running',
            claim_token   = :token,
            lease_expires = now() + make_interval(secs => :lease),
            started_at    = coalesce(started_at, now()),
            attempts      = attempts + 1
          WHERE id = (
            SELECT id FROM download_jobs
             WHERE status = 'queued'
               AND (lease_expires IS NULL OR lease_expires < now())
             ORDER BY priority, created_at
             FOR UPDATE SKIP LOCKED
             LIMIT 1
          )
        RETURNING id, user_id, link, site, tier, priority, status, attempts,
                  claim_token, lease_expires, created_at, started_at, finished_at
        """
    ).bindparams(token=uuid.uuid4(), lease=lease_seconds)

    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return None

    get_metrics().incr("queue_claims")

    # Return the session-tracked instance (not a detached copy) so later
    # mutations in complete()/fail() actually flush to the DB.  The raw
    # UPDATE already committed the new state; refresh() reloads it into the
    # identity-mapped object.
    job = await session.get(DownloadJob, row.id)
    await session.refresh(job)
    return job


# ── Finalize ────────────────────────────────────────────────────


async def complete(
    session: AsyncSession,
    job: DownloadJob,
    *,
    bytes_downloaded: int | None = None,
    filename: str | None = None,
) -> None:
    """Mark a job done, write history, and record metrics."""
    job.status = DONE
    job.finished_at = datetime.now(timezone.utc)
    job.total_bytes = bytes_downloaded

    history = DownloadHistory(
        user_id=job.user_id,
        job_id=job.id,
        link=job.link,
        site=job.site,
        filename=filename,
        bytes=bytes_downloaded,
        status=DONE,
    )
    session.add(history)

    get_metrics().incr("downloads_completed")
    if bytes_downloaded:
        get_metrics().incr("bytes_downloaded", bytes_downloaded)


async def fail(
    session: AsyncSession,
    job: DownloadJob,
    *,
    retry: bool = True,
) -> bool:
    """Mark a job failed.  If *retry* and attempts < max, re-queue it.

    Returns ``True`` when the job was re-queued for retry.
    """
    settings = get_settings()
    job.finished_at = datetime.now(timezone.utc)

    if retry and job.attempts < settings.job_max_attempts:
        job.status = QUEUED
        job.claim_token = None
        job.lease_expires = None
        job.started_at = None
        get_metrics().incr("downloads_failed")
        return True

    job.status = FAILED
    history = DownloadHistory(
        user_id=job.user_id,
        job_id=job.id,
        link=job.link,
        site=job.site,
        status=FAILED,
    )
    session.add(history)
    get_metrics().incr("downloads_failed")
    return False


async def cancel(session: AsyncSession, job_id: int) -> bool:
    """Cancel a queued job.  Returns ``True`` if the job was queued."""
    result = await session.execute(
        update(DownloadJob)
        .where(DownloadJob.id == job_id, DownloadJob.status == QUEUED)
        .values(status=CANCELLED, finished_at=datetime.now(timezone.utc))
    )
    if result.rowcount:
        get_metrics().incr("downloads_cancelled")
    return (result.rowcount or 0) > 0


async def cancel_user_queued(session: AsyncSession, user_id: int) -> int:
    """Cancel all queued jobs for a user.  Returns count cancelled."""
    result = await session.execute(
        update(DownloadJob)
        .where(DownloadJob.user_id == user_id, DownloadJob.status == QUEUED)
        .values(status=CANCELLED, finished_at=datetime.now(timezone.utc))
    )
    count = result.rowcount or 0
    if count:
        get_metrics().incr("downloads_cancelled", count)
    return count


# ── Reclaim (scheduler) ─────────────────────────────────────────


async def reclaim_expired(session: AsyncSession) -> int:
    """Re-queue jobs whose lease has expired (worker crash recovery).

    Returns the number of jobs re-queued.
    """
    result = await session.execute(
        update(DownloadJob)
        .where(
            DownloadJob.status == RUNNING,
            DownloadJob.lease_expires < datetime.now(timezone.utc),
        )
        .values(
            status=QUEUED,
            claim_token=None,
            lease_expires=None,
            started_at=None,
        )
    )
    count = result.rowcount or 0
    if count:
        get_metrics().incr("queue_reclaims", count)
    return count


# ── Queries ─────────────────────────────────────────────────────


async def get_job(session: AsyncSession, job_id: int) -> DownloadJob | None:
    """Fetch a job by id."""
    return await session.get(DownloadJob, job_id)


async def user_active_count(session: AsyncSession, user_id: int) -> int:
    """Count queued + running jobs for a user."""
    result = await session.scalar(
        select(func.count())
        .select_from(DownloadJob)
        .where(
            DownloadJob.user_id == user_id,
            DownloadJob.status.in_([QUEUED, RUNNING]),
        )
    )
    return result or 0


async def queue_depth(session: AsyncSession) -> int:
    """Total queued jobs (for /stats)."""
    result = await session.scalar(
        select(func.count())
        .select_from(DownloadJob)
        .where(DownloadJob.status == QUEUED)
    )
    return result or 0
