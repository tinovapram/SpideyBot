"""Daily usage and the gates built on it (FR-3.2, FR-15, spec 8.5).

Usage is one row per user-day, so every update is a single atomic statement rather than a
read-modify-write two concurrent submissions could interleave. Reserving a download is
therefore ``INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING``: a row comes back
only when the increment was allowed, which makes the check and the increment the same
operation. That property is the reason for Postgres (D4) and for the shape of this module.

``bytes_in`` is what the user's request pulled down, and it is what the bandwith cap is
measured against; ``bytes_out`` records what was sent back, for the admin view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ReferralBonus, UsageDaily

BYTES_PER_MB = 1024 * 1024

_SIZE_UNITS = ("KB", "MB", "GB", "TB")


def human_bytes(count: int) -> str:
    """A byte count as the usage copy expects it: ``0 B``, ``512 B``, ``1.5 KB``, ``2 GB``.

    Binary units, because the caps it is measured against are binary — ``daily_mb`` means 1024
    KiB, and printing a decimal MB would show a user over the cap they are being held to.
    """
    value = float(max(count, 0))
    if value < 1024:
        return f"{int(value)} B"

    # Each iteration renders the value in the current unit and returns as soon as it fits;
    # the value only ever shrinks, so the first unit that fits is the right one.
    for unit in _SIZE_UNITS:
        value /= 1024
        rendered = f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
        if value < 1024:
            return rendered
    return rendered


def today(now: datetime | None = None) -> date:
    """The current usage day.

    UTC, because the boundary is UTC-based (spec 8.5) — a local-timezone day would reset
    at a moment the operator could not predict from the documentation.
    """
    return (now or datetime.now(UTC)).astimezone(UTC).date()


@dataclass(frozen=True)
class Usage:
    """A day's counters. Absent row and all-zero row mean the same thing."""

    downloads: int = 0
    bonus_downloads: int = 0
    bytes_in: int = 0
    bytes_out: int = 0

    @property
    def bytes_total(self) -> int:
        return self.bytes_in + self.bytes_out


async def current(db: AsyncSession, user_id: int, *, day: date | None = None) -> Usage:
    """Today's usage, or zeros when nothing has been recorded yet."""
    row = await db.get(UsageDaily, (user_id, day or today()))
    if row is None:
        return Usage()
    return Usage(
        downloads=row.downloads,
        bonus_downloads=row.bonus_downloads,
        bytes_in=row.bytes_in,
        bytes_out=row.bytes_out,
    )


async def active_bonus_credits(
    db: AsyncSession, user_id: int, *, now: datetime | None = None
) -> int:
    """Referral credits still inside their 30-day window (FR-17.5).

    Counted live rather than cached, so a credit stops counting the moment it expires.
    """
    moment = now or datetime.now(UTC)
    result = await db.execute(
        select(func.count())
        .select_from(ReferralBonus)
        .where(ReferralBonus.referrer_id == user_id, ReferralBonus.expires_at > moment)
    )
    return int(result.scalar_one())


def bonus_allowance(settings: Settings, credits: int) -> int:
    """Extra downloads per day contributed by referral credits."""
    return credits * settings.referral.daily_bonus


def download_allowance(settings: Settings, *, tier_daily_downloads: int, credits: int) -> int:
    """The day's download allowance: the tier's, plus any active referral bonus."""
    return tier_daily_downloads + bonus_allowance(settings, credits)


def remaining(limit: int, used: int) -> int | None:
    """What is left, or ``None`` for an unlimited dimension.

    ``0`` means unlimited throughout the tier table (spec 13), so it must never be
    reported as "0 remaining".
    """
    if limit <= 0:
        return None
    return max(0, limit - used)


def bandwidth_exhausted(usage: Usage, daily_mb: int) -> bool:
    """Whether today's downloads have already reached the bandwidth cap."""
    if daily_mb <= 0:
        return False
    return usage.bytes_in >= daily_mb * BYTES_PER_MB


async def reserve_download(
    db: AsyncSession,
    user_id: int,
    *,
    limit: int,
    bonus: int = 0,
    day: date | None = None,
) -> bool:
    """Claim one download from today's allowance, atomically.

    Returns ``False`` when the allowance is already spent. Two submissions racing cannot
    both be told yes, because the limit is part of the same statement (FR-3.2).
    """
    if limit < 1:
        # 0 is read literally for a count, unlike a size limit where 0 means unlimited.
        return False

    statement = (
        pg_insert(UsageDaily)
        .values(user_id=user_id, day=day or today(), downloads=1, bonus_downloads=bonus)
        .on_conflict_do_update(
            index_elements=[UsageDaily.user_id, UsageDaily.day],
            set_={"downloads": UsageDaily.downloads + 1},
            # The gate lives on the DO UPDATE, so a conflicting row is only incremented
            # while it is under the limit; RETURNING then yields nothing when it is not.
            where=UsageDaily.downloads < limit,
        )
        .returning(UsageDaily.downloads)
    )
    return (await db.execute(statement)).scalar_one_or_none() is not None


async def release_download(db: AsyncSession, user_id: int, *, day: date | None = None) -> None:
    """Hand back a reservation whose job failed or was cancelled before delivering.

    Floored at zero: a job that fails before its reservation is durable must not be able
    to drive the counter negative and mint free downloads for the rest of the day.
    """
    usage_day = day or today()
    await db.execute(
        update(UsageDaily)
        .where(
            UsageDaily.user_id == user_id,
            UsageDaily.day == usage_day,
            UsageDaily.downloads > 0,
        )
        .values(downloads=UsageDaily.downloads - 1)
    )


async def record_bytes(
    db: AsyncSession,
    user_id: int,
    *,
    inbound: int = 0,
    outbound: int = 0,
    day: date | None = None,
) -> None:
    """Add transferred bytes to today's row (FR-3.2 bandwidth accounting)."""
    statement = (
        pg_insert(UsageDaily)
        .values(user_id=user_id, day=day or today(), bytes_in=inbound, bytes_out=outbound)
        .on_conflict_do_update(
            index_elements=[UsageDaily.user_id, UsageDaily.day],
            set_={
                "bytes_in": UsageDaily.bytes_in + inbound,
                "bytes_out": UsageDaily.bytes_out + outbound,
            },
        )
    )
    await db.execute(statement)
