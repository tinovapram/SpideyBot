"""Daily usage, and the atomicity the quota gates depend on.

These run against a real Postgres because the property that matters most — a reservation
cannot oversell the allowance under concurrency — comes from the database, not from Python.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app import db, quota, users
from app.models import ReferralBonus

DAY = quota.today(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
YESTERDAY = DAY - timedelta(days=1)


async def _register(user_id: int) -> None:
    async with db.session() as session:
        await users.upsert(session, user_id=user_id, username=f"u{user_id}", first_name="Test")


# ── reserving and releasing ─────────────────────────────────────────────


async def test_first_reservation_creates_the_row(session) -> None:
    await users.upsert(session, user_id=1, username="a", first_name="A")

    assert await quota.reserve_download(session, 1, limit=3, day=DAY) is True

    assert (await quota.current(session, 1, day=DAY)).downloads == 1


async def test_reservations_stop_at_the_limit(session) -> None:
    await users.upsert(session, user_id=1, username="a", first_name="A")

    granted = [await quota.reserve_download(session, 1, limit=2, day=DAY) for _ in range(4)]

    assert granted == [True, True, False, False]
    assert (await quota.current(session, 1, day=DAY)).downloads == 2


async def test_released_reservation_is_available_again(session) -> None:
    await users.upsert(session, user_id=1, username="a", first_name="A")
    await quota.reserve_download(session, 1, limit=1, day=DAY)

    await quota.release_download(session, 1, day=DAY)

    assert (await quota.current(session, 1, day=DAY)).downloads == 0
    assert await quota.reserve_download(session, 1, limit=1, day=DAY) is True


async def test_release_cannot_go_negative(session) -> None:
    """Otherwise a failed job could mint free downloads for the rest of the day."""
    await users.upsert(session, user_id=1, username="a", first_name="A")

    await quota.release_download(session, 1, day=DAY)
    await quota.release_download(session, 1, day=DAY)

    assert (await quota.current(session, 1, day=DAY)).downloads == 0


async def test_quota_is_per_user_and_per_day(session) -> None:
    await users.upsert(session, user_id=1, username="a", first_name="A")
    await users.upsert(session, user_id=2, username="b", first_name="B")

    await quota.reserve_download(session, 1, limit=5, day=DAY)
    await quota.reserve_download(session, 1, limit=5, day=DAY)

    assert (await quota.current(session, 1, day=DAY)).downloads == 2
    assert (await quota.current(session, 2, day=DAY)).downloads == 0
    # Yesterday is a different allowance, which is what makes the reset free (spec 8.5).
    assert (await quota.current(session, 1, day=YESTERDAY)).downloads == 0


async def test_the_days_bonus_is_snapshotted_on_first_use(session) -> None:
    await users.upsert(session, user_id=1, username="a", first_name="A")

    await quota.reserve_download(session, 1, limit=20, bonus=10, day=DAY)

    assert (await quota.current(session, 1, day=DAY)).bonus_downloads == 10


# ── the property that needs Postgres: no overselling ────────────────────


async def test_concurrent_reservations_cannot_oversell(engine) -> None:
    """The check and the increment are one statement, so a race cannot hand out too many.

    Ten attempts through *separate* sessions against a limit of three must yield exactly
    three grants. A read-then-write implementation fails this.
    """
    await _register(42)

    async def attempt() -> bool:
        async with db.session() as session:
            return await quota.reserve_download(session, 42, limit=3, day=DAY)

    results = await asyncio.gather(*(attempt() for _ in range(10)))

    assert sum(results) == 3

    async with db.session() as session:
        assert (await quota.current(session, 42, day=DAY)).downloads == 3


# ── bandwidth ───────────────────────────────────────────────────────────


async def test_recorded_bytes_accumulate(session) -> None:
    await users.upsert(session, user_id=1, username="a", first_name="A")

    await quota.record_bytes(session, 1, inbound=1000, outbound=200, day=DAY)
    await quota.record_bytes(session, 1, inbound=500, outbound=100, day=DAY)

    usage = await quota.current(session, 1, day=DAY)
    assert usage.bytes_in == 1500
    assert usage.bytes_out == 300
    assert usage.bytes_total == 1800


# ── referral bonuses, counted live ──────────────────────────────────────


async def test_expired_credits_stop_counting(session, settings) -> None:
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    await users.upsert(session, user_id=1, username="a", first_name="A")
    await users.upsert(session, user_id=2, username="b", first_name="B")
    session.add_all(
        [
            ReferralBonus(
                referrer_id=1, invitee_id=2, credited_at=now, expires_at=now + timedelta(days=1)
            ),
            ReferralBonus(
                referrer_id=1, invitee_id=2, credited_at=now, expires_at=now - timedelta(days=1)
            ),
        ]
    )
    await session.flush()

    credits = await quota.active_bonus_credits(session, 1, now=now)

    assert credits == 1
    assert quota.bonus_allowance(settings, credits) == settings.referral.daily_bonus
    assert quota.download_allowance(settings, tier_daily_downloads=10, credits=credits) == 20


async def test_no_credits_means_no_bonus(session, settings) -> None:
    await users.upsert(session, user_id=1, username="a", first_name="A")

    assert await quota.active_bonus_credits(session, 1) == 0
    assert quota.download_allowance(settings, tier_daily_downloads=10, credits=0) == 10


# ── pure helpers, kept beside the day arithmetic they belong to ─────────


@pytest.mark.parametrize(
    ("limit", "used", "expected"),
    [(10, 0, 10), (10, 10, 0), (10, 11, 0), (0, 5, None), (-1, 5, None)],
)
def test_remaining_reports_unlimited_as_none(limit: int, used: int, expected: int | None) -> None:
    assert quota.remaining(limit, used) == expected


def test_the_day_boundary_is_utc() -> None:
    """A local-timezone day would reset at an hour the documentation never mentions."""
    late = datetime(2026, 3, 1, 23, 30, tzinfo=UTC)
    early = datetime(2026, 3, 2, 0, 30, tzinfo=UTC)

    assert quota.today(late) != quota.today(early)


def test_bandwidth_gate_uses_downloaded_bytes() -> None:
    usage = quota.Usage(bytes_in=5 * quota.BYTES_PER_MB)

    assert quota.bandwidth_exhausted(usage, daily_mb=5) is True
    assert quota.bandwidth_exhausted(usage, daily_mb=6) is False
    assert quota.bandwidth_exhausted(usage, daily_mb=0) is False  # 0 means unlimited
