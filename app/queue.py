"""The job queue: priority ordering, per-tier slots, and leases (FR-10, spec 8.1-8.2).

Waiting is *durable state*, not an in-memory list, because a restart must not lose a queued
job or re-run a delivered one (FR-10.5, E26). Two mechanisms carry that:

* a running job holds a **lease** rather than an open transaction. A row lock held across a
  multi-minute download would pin a connection and, worse, leave the job marked running
  forever if the process died mid-transfer. A lease expires instead, and the sweep decides
  what to do with the job (plan D15).
* claiming takes the oldest *claimable* row with ``FOR UPDATE SKIP LOCKED``, so two workers
  never take the same job and neither blocks on the other. That clause is why the project is
  Postgres-only (D4) — it is a concurrency guarantee, not an optimization.

The slot check travels inside the claiming statement rather than preceding it as a separate
read. A check-then-claim could race: two claims would each see the last free slot, then both
take it, which is precisely the oversell FR-10.1 forbids. It rides along as a correlated
count, so the limit that applies is the one for *that job's* tier — which is what lets a
single global claimer serve every tier correctly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import Settings
from app.models import Job
from app.tiers import TIER_NAMES, get_limits

QUEUED = "queued"
RUNNING = "running"
DELIVERED = "delivered"
FAILED = "failed"
CANCELLED = "cancelled"

#: States a job never leaves. Delivering twice is what E26 forbids, so every terminal
#: transition is guarded by this set rather than by the caller's bookkeeping.
TERMINAL_STATES = frozenset({DELIVERED, FAILED, CANCELLED})

#: States a job is still interesting in: waiting for a slot, or holding one (FR-10.6).
ACTIVE_STATES = (QUEUED, RUNNING)

CancelKind = Literal["cancelled", "requested", "unknown", "not_owned", "finished"]


def _now() -> datetime:
    return datetime.now(UTC)


def _slot_limits(settings: Settings):
    """A SQL expression for the slot count that applies to a job's own tier (FR-10.1).

    Expressed in SQL so the limit can be compared against a correlated count in the same
    statement. Admin never reaches here: it resolves to the premium tier and the highest
    priority before a job is enqueued (spec 2.1).
    """
    limits = {name: get_limits(settings, name).concurrent for name in TIER_NAMES}
    return case(limits, value=Job.tier, else_=get_limits(settings, "free").concurrent)


async def enqueue(
    db: AsyncSession,
    *,
    user_id: int,
    link: str,
    tier: str = "free",
    priority: float = 0.0,
    site: str | None = None,
) -> Job:
    """Accept a job. It waits here until a slot frees — it is not rejected for concurrency
    (FR-10.2), and it outlives the process that accepted it (FR-10.5).
    """
    job = Job(
        user_id=user_id,
        link=link,
        site=site,
        tier=tier,
        priority=priority,
        status=QUEUED,
    )
    db.add(job)
    await db.flush()
    return job


async def claim(
    db: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    lease_seconds: int | None = None,
) -> Job | None:
    """Take the next runnable job, or ``None`` when nothing is runnable right now.

    Nothing being runnable is the ordinary idle case, not an error: either the queue is
    empty, or every waiting job belongs to a user already at their slot count. The second
    case is why this cannot simply take the head of the queue — skipping such a job and
    running the next user's is what makes a busy user wait without stalling everyone else.
    """
    moment = now or _now()
    lease = lease_seconds if lease_seconds is not None else settings.runtime.job_lease_seconds

    # Aliases, so these counts do not silently correlate with the row being selected.
    holder = aliased(Job)
    slots_taken = (
        select(func.count())
        .select_from(holder)
        .where(holder.user_id == Job.user_id, holder.status == RUNNING)
        .scalar_subquery()
    )
    active = aliased(Job)
    running_total = (
        select(func.count()).select_from(active).where(active.status == RUNNING).scalar_subquery()
    )

    statement = (
        select(Job)
        .where(
            Job.status == QUEUED,
            slots_taken < _slot_limits(settings),
            running_total < settings.runtime.max_concurrent_downloads,
        )
        .order_by(Job.priority, Job.created_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=Job)
    )
    job = (await db.execute(statement)).scalar_one_or_none()
    if job is None:
        return None

    job.status = RUNNING
    job.claim_token = str(uuid.uuid4())
    job.lease_expires = moment + timedelta(seconds=lease)
    job.attempts += 1
    if job.started_at is None:
        # Kept from the first attempt, so a resumed job still reports when it really began.
        job.started_at = moment
    await db.flush()
    return job


async def renew(
    db: AsyncSession,
    job: Job,
    settings: Settings,
    *,
    now: datetime | None = None,
    lease_seconds: int | None = None,
) -> bool:
    """Extend a lease that is still held by this claim.

    Guarded on ``claim_token`` so a worker whose lease already expired and was reclaimed by
    someone else cannot renew its way back into ownership. Returns whether the lease is
    still ours — a worker that gets ``False`` should stop touching the job.
    """
    if job.claim_token is None:
        return False

    moment = now or _now()
    lease = lease_seconds if lease_seconds is not None else settings.runtime.job_lease_seconds
    statement = (
        update(Job)
        .where(
            Job.id == job.id,
            Job.claim_token == job.claim_token,
            Job.status == RUNNING,
        )
        .values(lease_expires=moment + timedelta(seconds=lease))
        .returning(Job.id)
    )
    renewed = (await db.execute(statement)).scalar_one_or_none() is not None
    if renewed:
        job.lease_expires = moment + timedelta(seconds=lease)
    return renewed


@dataclass(frozen=True)
class Reclaim:
    """What the sweep did, by job id. Returned so startup can log it honestly."""

    requeued: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    cancelled: tuple[int, ...] = ()

    @property
    def total(self) -> int:
        return len(self.requeued) + len(self.failed) + len(self.cancelled)


async def reclaim_expired(
    db: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> Reclaim:
    """Recover jobs whose lease ran out, which is how a crashed worker is detected.

    A job is not blindly retried. A cancellation requested while it was abandoned is
    honoured rather than revived, and a job that has already used its attempts fails
    instead of looping forever — an unattended retry of something that kills its worker is
    a worse failure mode than reporting it (FR-10.5, D21).
    """
    moment = now or _now()
    expired = (
        Job.status == RUNNING,
        Job.lease_expires.is_not(None),
        Job.lease_expires < moment,
    )

    requested = Job.cancel_requested_at.is_not(None)
    exhausted = Job.attempts >= settings.runtime.job_max_attempts

    statement = (
        update(Job)
        .where(*expired)
        .values(
            status=case((requested, CANCELLED), (exhausted, FAILED), else_=QUEUED),
            # Re-queueing has to release the claim, or a stale token would still match and
            # the old worker could keep renewing a lease it no longer owns.
            claim_token=None,
            lease_expires=None,
            finished_at=case((or_(requested, exhausted), moment), else_=None),
        )
        .returning(Job.id, Job.status)
        # This sweep owns no ORM objects: it runs from its own session and reports ids. The
        # default would try to reconcile this session's identity map, and a CASE in SET is not
        # something SQLAlchemy can evaluate, so it would expire objects nobody asked it to.
        .execution_options(synchronize_session=False)
    )
    rows = (await db.execute(statement)).all()

    buckets: dict[str, list[int]] = {QUEUED: [], FAILED: [], CANCELLED: []}
    for job_id, status in rows:
        buckets[status].append(job_id)
    return Reclaim(
        requeued=tuple(buckets[QUEUED]),
        failed=tuple(buckets[FAILED]),
        cancelled=tuple(buckets[CANCELLED]),
    )


async def terminate(
    db: AsyncSession, job: Job, status: str, *, now: datetime | None = None
) -> bool:
    """Move a job to a terminal state, once. Returns whether this call was the one.

    Every terminal transition goes through here so that "never deliver twice" (E26) is a
    property of the queue rather than of each caller remembering to check. A job that
    already finished is left exactly as it is, which is what makes a resumed worker safe.
    """
    if status not in TERMINAL_STATES:
        raise ValueError(f"{status!r} is not a terminal job state")

    moment = now or _now()
    statement = (
        update(Job)
        .where(Job.id == job.id, Job.status.not_in(TERMINAL_STATES))
        .values(
            status=status,
            finished_at=moment,
            claim_token=None,
            lease_expires=None,
        )
        .returning(Job.id)
        # The caller's object is updated by hand below, so the identity-map reconciliation
        # would be a second, redundant pass over the same row.
        .execution_options(synchronize_session=False)
    )
    changed = (await db.execute(statement)).scalar_one_or_none() is not None
    if changed:
        # A Core UPDATE writes around the ORM, so the object callers still hold would
        # otherwise keep describing the pre-terminal row.
        job.status = status
        job.finished_at = moment
        job.claim_token = None
        job.lease_expires = None
    return changed


async def mark_delivered(db: AsyncSession, job: Job, *, now: datetime | None = None) -> bool:
    """Terminal success: at least one file reached the user (spec 8.1)."""
    return await terminate(db, job, DELIVERED, now=now)


async def mark_failed(db: AsyncSession, job: Job, *, now: datetime | None = None) -> bool:
    """Terminal failure: nothing was delivered. Releasable quota is the caller's business."""
    return await terminate(db, job, FAILED, now=now)


@dataclass(frozen=True)
class CancelOutcome:
    """What ``/cancel`` should tell the user, and the job it applies to."""

    kind: CancelKind
    job: Job | None = None


async def request_cancel(db: AsyncSession, *, job_id: int, user_id: int) -> CancelOutcome:
    """Ask for a job to stop, applying the spec's refusals (FR-11.3, FR-11.4).

    A queued job is cancelled outright — no worker will ever pick it up, because claiming
    only takes ``queued`` rows. A running job cannot be stopped from here: it is mid-transfer
    in another task, so the request is recorded and the worker notices at its next
    checkpoint. That indirection is what makes FR-11.6 achievable without pretending a
    database write can interrupt a download.
    """
    job = await db.get(Job, job_id)
    if job is None:
        return CancelOutcome("unknown")
    if job.user_id != user_id:
        # Owned by someone else is reported as such rather than as unknown, so the owner's
        # ids are not silently enumerable but a genuine mistake is explained.
        return CancelOutcome("not_owned", job)
    if job.status in TERMINAL_STATES:
        return CancelOutcome("finished", job)

    moment = _now()
    if job.status == QUEUED:
        await terminate(db, job, CANCELLED, now=moment)
        return CancelOutcome("cancelled", job)

    job.cancel_requested_at = moment
    await db.flush()
    return CancelOutcome("requested", job)


@dataclass(frozen=True)
class JobCounts:
    """Distinguishing waiting from running is FR-10.6; the numbers serve ``/quota``."""

    running: int = 0
    queued: int = 0

    @property
    def active(self) -> int:
        return self.running + self.queued


async def counts(db: AsyncSession, user_id: int) -> JobCounts:
    """How many of this user's jobs are running and how many are waiting."""
    result = await db.execute(
        select(Job.status, func.count())
        .where(Job.user_id == user_id, Job.status.in_(ACTIVE_STATES))
        .group_by(Job.status)
    )
    tally = {status: count for status, count in result.all()}
    return JobCounts(running=tally.get(RUNNING, 0), queued=tally.get(QUEUED, 0))


async def active_jobs(db: AsyncSession, user_id: int) -> list[Job]:
    """This user's unfinished jobs, in the order they are described to them.

    Running first, because those are the ones with progress to report; then submission
    order, which is the same order the queue would start them in (FR-12.2).
    """
    result = await db.execute(
        select(Job)
        .where(Job.user_id == user_id, Job.status.in_(ACTIVE_STATES))
        .order_by(Job.status.desc(), Job.created_at, Job.id)
    )
    return list(result.scalars().all())
