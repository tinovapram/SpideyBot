"""The leased queue: ordering, per-tier slots, and the guarantees restart depends on.

Real Postgres, because every property worth testing here is a database property: that two
claims cannot take the same job (``FOR UPDATE SKIP LOCKED``), that the slot count travels with
the claim rather than preceding it, and that a terminal state is terminal. A fake would pass
these tests while the real implementation was broken.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app import db, queue, users
from app.config import Settings
from app.models import Job

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LINK = "https://example.test/album/1"


def _after(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


def _with_runtime(settings: Settings, **overrides: int) -> Settings:
    """Settings with a runtime knob overridden, for the caps a test needs to hit."""
    return settings.model_copy(update={"runtime": settings.runtime.model_copy(update=overrides)})


async def _register(*user_ids: int) -> None:
    async with db.session() as session:
        for user_id in user_ids:
            await users.upsert(session, user_id=user_id, username=f"u{user_id}", first_name="T")


async def _expire(session, job, settings: Settings) -> None:
    """Push a running job's lease into the past, as a crashed worker would leave it."""
    job.lease_expires = _after(-settings.runtime.job_lease_seconds - 1)
    await session.flush()


# ── ordering (FR-10.4) ──────────────────────────────────────────────────


async def test_a_new_job_waits_as_queued(session) -> None:
    await _register(1)

    job = await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)

    assert job.id is not None
    assert job.status == queue.QUEUED
    assert job.claim_token is None
    assert job.attempts == 0


async def test_claim_takes_the_highest_priority_first(session, settings) -> None:
    """Free is submitted first and still waits: priority outranks submission order."""
    await _register(1, 2)

    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    paid = await queue.enqueue(session, user_id=2, link=LINK, tier="premium", priority=0.5)

    claimed = await queue.claim(session, settings, now=NOW)

    assert claimed is not None
    assert claimed.id == paid.id


async def test_equal_priority_is_served_in_submission_order(session, settings) -> None:
    await _register(1, 2)

    first = await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    await queue.enqueue(session, user_id=2, link=LINK, tier="free", priority=2.0)

    claimed = await queue.claim(session, settings, now=NOW)

    assert claimed is not None
    assert claimed.id == first.id


# ── claiming (FR-10.1, FR-10.3) ─────────────────────────────────────────


async def test_claim_records_the_lease_it_took(session, settings) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)

    claimed = await queue.claim(session, settings, now=NOW)

    assert claimed is not None
    assert claimed.status == queue.RUNNING
    assert claimed.claim_token is not None
    assert claimed.lease_expires == _after(settings.runtime.job_lease_seconds)
    assert claimed.attempts == 1
    assert claimed.started_at == NOW


async def test_claim_skips_a_user_who_is_already_holding_their_slot(session, settings) -> None:
    """The second job is next in line, but its owner has no free slot (FR-10.1).

    Taking the head of the queue here would oversell the free tier's single slot, which is
    the whole reason the slot count is part of the claiming statement.
    """
    await _register(1, 2)

    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    waiting = await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    other = await queue.enqueue(session, user_id=2, link=LINK, tier="free", priority=2.0)

    first = await queue.claim(session, settings, now=NOW)
    second = await queue.claim(session, settings, now=NOW)

    assert first is not None and second is not None
    assert first.id != waiting.id
    assert second.id == other.id


async def test_claim_finds_nothing_when_every_waiting_job_is_blocked(session, settings) -> None:
    await _register(1)

    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)

    assert await queue.claim(session, settings, now=NOW) is not None
    assert await queue.claim(session, settings, now=NOW) is None


async def test_the_slot_count_follows_the_tier(session, settings) -> None:
    """Pro has two slots, so the same shape of queue drains further than free's."""
    await _register(1)

    for _ in range(2):
        await queue.enqueue(session, user_id=1, link=LINK, tier="pro", priority=1.0)

    first = await queue.claim(session, settings, now=NOW)
    second = await queue.claim(session, settings, now=NOW)
    third = await queue.claim(session, settings, now=NOW)

    assert first is not None and second is not None
    assert third is None


async def test_a_global_cap_stops_claiming_across_users(clean, settings) -> None:
    capped = _with_runtime(settings, max_concurrent_downloads=1)
    await _register(1, 2)

    async with db.session() as session:
        await queue.enqueue(session, user_id=1, link=LINK, tier="premium", priority=0.5)
        await queue.enqueue(session, user_id=2, link=LINK, tier="premium", priority=0.5)

    async with db.session() as session:
        assert await queue.claim(session, capped, now=NOW) is not None
    async with db.session() as session:
        assert await queue.claim(session, capped, now=NOW) is None


async def test_concurrent_claims_never_hand_out_the_same_job(clean, settings) -> None:
    """Four claimers, four jobs, one winner each — the reason for ``SKIP LOCKED``.

    Each claimer uses its own connection and its own transaction, so without the clause they
    would block on each other or hand the same row to two workers.
    """
    await _register(1, 2, 3, 4)
    async with db.session() as session:
        for user_id in (1, 2, 3, 4):
            await queue.enqueue(session, user_id=user_id, link=LINK, tier="free", priority=2.0)

    async def claim_once():
        async with db.session() as session:
            return await queue.claim(session, settings, now=NOW)

    claimed = await asyncio.gather(*(claim_once() for _ in range(4)))

    ids = [job.id for job in claimed if job is not None]
    assert len(ids) == 4
    assert len(set(ids)) == 4


# ── leases (FR-10.5, D15) ───────────────────────────────────────────────


async def test_renewing_pushes_the_deadline_out(session, settings) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)

    assert claimed is not None
    assert await queue.renew(session, claimed, settings, now=_after(60)) is True

    assert claimed.lease_expires == _after(60 + settings.runtime.job_lease_seconds)


async def test_renewal_is_refused_once_the_claim_is_not_ours(session, settings) -> None:
    """Otherwise a worker whose lease was reclaimed could renew its way back into owning
    a job that another worker is now running."""
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None

    # Another worker reclaimed the abandoned job and took a claim of its own. Written
    # straight to the row, with in-memory sync off, so this session's object still holds
    # the old token and the renewal has to fail on the database's answer rather than on
    # Python's bookkeeping.
    await session.execute(
        update(Job)
        .where(Job.id == claimed.id)
        .values(claim_token=str(uuid.uuid4()))
        .execution_options(synchronize_session=False)
    )

    assert await queue.renew(session, claimed, settings, now=_after(60)) is False


async def test_an_abandoned_job_goes_back_to_the_queue(session, settings) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None
    await _expire(session, claimed, settings)

    outcome = await queue.reclaim_expired(session, settings, now=NOW)
    await session.refresh(claimed)

    assert outcome.requeued == (claimed.id,)
    assert claimed.status == queue.QUEUED
    assert claimed.claim_token is None
    assert claimed.lease_expires is None
    # The attempt it used is remembered, so the retry budget cannot grow on every crash.
    assert claimed.attempts == 1


async def test_a_live_lease_is_left_alone(session, settings) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None

    outcome = await queue.reclaim_expired(session, settings, now=_after(1))

    assert outcome.total == 0
    assert claimed.status == queue.RUNNING


async def test_a_job_that_used_up_its_attempts_fails_instead_of_looping(session, settings) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None
    claimed.attempts = settings.runtime.job_max_attempts
    await _expire(session, claimed, settings)

    outcome = await queue.reclaim_expired(session, settings, now=NOW)
    await session.refresh(claimed)

    assert outcome.failed == (claimed.id,)
    assert claimed.status == queue.FAILED
    assert claimed.finished_at == NOW


async def test_requeued_work_can_be_claimed_again(session, settings) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None
    await _expire(session, claimed, settings)
    await queue.reclaim_expired(session, settings, now=NOW)

    again = await queue.claim(session, settings, now=_after(5))

    assert again is not None
    assert again.id == claimed.id
    assert again.attempts == 2


# ── terminal states (E26) ───────────────────────────────────────────────


async def test_a_delivered_job_cannot_be_delivered_twice(session, settings) -> None:
    """The ledger's guarantee, expressed once here so no caller has to re-derive it."""
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None

    assert await queue.mark_delivered(session, claimed, now=NOW) is True
    assert await queue.mark_delivered(session, claimed, now=_after(10)) is False

    assert claimed.status == queue.DELIVERED
    assert claimed.finished_at == NOW


async def test_a_cancelled_job_cannot_be_revived_by_a_failure(session, settings) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None
    await queue.terminate(session, claimed, queue.CANCELLED, now=NOW)

    assert await queue.mark_failed(session, claimed, now=_after(10)) is False
    assert claimed.status == queue.CANCELLED


async def test_terminating_with_a_non_terminal_state_is_a_programming_error(
    session, settings
) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None

    with pytest.raises(ValueError, match="terminal"):
        await queue.terminate(session, claimed, queue.RUNNING, now=NOW)


# ── cancellation (FR-11.3, FR-11.4, FR-11.6) ────────────────────────────


async def test_cancelling_a_waiting_job_takes_effect_immediately(session, settings) -> None:
    """No worker will pick it up, so it can be closed here rather than flagged for one."""
    await _register(1)
    job = await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)

    outcome = await queue.request_cancel(session, job_id=job.id, user_id=1)

    assert outcome.kind == "cancelled"
    assert job.status == queue.CANCELLED
    assert await queue.claim(session, settings, now=NOW) is None


async def test_cancelling_a_running_job_records_a_request(session, settings) -> None:
    """A database write cannot interrupt a download, so the worker is asked to stop."""
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None

    outcome = await queue.request_cancel(session, job_id=claimed.id, user_id=1)

    assert outcome.kind == "requested"
    assert claimed.status == queue.RUNNING
    assert claimed.cancel_requested_at is not None


async def test_a_cancellation_requested_while_abandoned_is_honoured(session, settings) -> None:
    """The worker died before it could act on the request; reviving the job would override
    a cancellation the user already asked for."""
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None
    await queue.request_cancel(session, job_id=claimed.id, user_id=1)
    await _expire(session, claimed, settings)

    outcome = await queue.reclaim_expired(session, settings, now=NOW)
    await session.refresh(claimed)

    assert outcome.cancelled == (claimed.id,)
    assert claimed.status == queue.CANCELLED


async def test_cancelling_an_unknown_id_is_reported_as_unknown(session) -> None:
    await _register(1)

    assert (await queue.request_cancel(session, job_id=999, user_id=1)).kind == "unknown"


async def test_another_users_job_cannot_be_cancelled(session) -> None:
    await _register(1, 2)
    job = await queue.enqueue(session, user_id=2, link=LINK, tier="free", priority=2.0)

    outcome = await queue.request_cancel(session, job_id=job.id, user_id=1)

    assert outcome.kind == "not_owned"
    assert job.status == queue.QUEUED


async def test_a_finished_job_reports_itself_as_finished(session, settings) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None
    await queue.mark_delivered(session, claimed, now=NOW)

    assert (await queue.request_cancel(session, job_id=claimed.id, user_id=1)).kind == "finished"


# ── what the user is shown (FR-10.6, FR-12) ─────────────────────────────


async def test_counts_separate_waiting_from_running(session, settings) -> None:
    await _register(1)
    for _ in range(3):
        await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    await queue.claim(session, settings, now=NOW)

    tally = await queue.counts(session, 1)

    assert tally == queue.JobCounts(running=1, queued=2)
    assert tally.active == 3


async def test_finished_jobs_are_not_counted_as_active(session, settings) -> None:
    await _register(1)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None
    await queue.mark_delivered(session, claimed, now=NOW)

    assert await queue.counts(session, 1) == queue.JobCounts()


async def test_active_jobs_are_listed_running_first(session, settings) -> None:
    await _register(1)
    first = await queue.enqueue(session, user_id=1, link=LINK, tier="pro", priority=1.0)
    second = await queue.enqueue(session, user_id=1, link=LINK, tier="pro", priority=1.0)
    claimed = await queue.claim(session, settings, now=NOW)
    assert claimed is not None and claimed.id == first.id

    listed = await queue.active_jobs(session, 1)

    assert [job.id for job in listed] == [first.id, second.id]


async def test_another_users_jobs_are_not_listed(session) -> None:
    await _register(1, 2)
    await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)

    assert await queue.active_jobs(session, 2) == []
