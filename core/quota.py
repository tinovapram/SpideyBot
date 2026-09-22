"""Quota check / consume / reset — atomic SQL against PostgreSQL.

Every public function takes an :class:`AsyncSession` and operates within the
caller's transaction.  All counters live in ``quota_usage`` with a composite
key ``(user_id, period_start, period_type)`` where *period_type* is ``daily``
or ``monthly``.  Upserts are atomic via ``ON CONFLICT DO UPDATE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import QuotaUsage, User
from core.tiers import QuotaPolicy, effective_tier, tier_policy

# ── Types ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class QuotaSnapshot:
    """Read-only view of the user's current usage vs. their tier policy."""

    user_id: int
    tier: str
    policy: QuotaPolicy
    downloads_today: int
    bytes_today: int
    bytes_this_month: int
    active_downloads: int
    referral_bonus: int

    @property
    def downloads_remaining(self) -> int | None:
        if self.policy.daily_downloads is None:
            return None
        return max(0, self.policy.daily_downloads + self.referral_bonus - self.downloads_today)

    @property
    def daily_bytes_remaining(self) -> int | None:
        if self.policy.daily_bytes is None:
            return None
        return max(0, self.policy.daily_bytes - self.bytes_today)

    @property
    def monthly_bytes_remaining(self) -> int | None:
        if self.policy.monthly_bytes is None:
            return None
        return max(0, self.policy.monthly_bytes - self.bytes_this_month)

    @property
    def can_download(self) -> bool:
        """True when the user has at least one download and byte remaining."""
        dl = self.downloads_remaining
        if dl is not None and dl <= 0:
            return False
        db = self.daily_bytes_remaining
        if db is not None and db <= 0:
            return False
        mb = self.monthly_bytes_remaining
        if mb is not None and mb <= 0:
            return False
        if self.policy.concurrent <= self.active_downloads:
            return False
        return True


class QuotaExceeded(Exception):
    """Raised when a user's quota does not allow a new download."""

    def __init__(self, reason: str, snapshot: QuotaSnapshot | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot


# ── Internal helpers ────────────────────────────────────────────


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _month_start_utc() -> date:
    today = _today_utc()
    return today.replace(day=1)


async def _upsert_counter(
    session: AsyncSession,
    user_id: int,
    period_start: date,
    period_type: str,
) -> QuotaUsage:
    """Insert-or-get a quota_usage row (no increment)."""
    stmt = (
        text(
            """
            INSERT INTO quota_usage (user_id, period_start, period_type, downloads, bytes_down, bytes_up)
            VALUES (:uid, :ps, :pt, 0, 0, 0)
            ON CONFLICT (user_id, period_start, period_type) DO NOTHING
            """
        )
        .bindparams(uid=user_id, ps=period_start, pt=period_type)
    )
    await session.execute(stmt)
    await session.flush()
    row = await session.get(QuotaUsage, (user_id, period_start, period_type))
    assert row is not None
    return row


async def _atomic_increment(
    session: AsyncSession,
    user_id: int,
    period_start: date,
    period_type: str,
    *,
    add_downloads: int = 0,
    add_bytes: int = 0,
) -> QuotaUsage:
    """Atomically increment counters and return the updated row.

    Uses ``UPDATE ... RETURNING`` so the caller sees the post-increment values
    within the same transaction.
    """
    await _upsert_counter(session, user_id, period_start, period_type)
    stmt = (
        text(
            """
            UPDATE quota_usage
               SET downloads   = downloads   + :dl,
                   bytes_down  = bytes_down  + :bd
             WHERE user_id      = :uid
               AND period_start = :ps
               AND period_type  = :pt
            RETURNING user_id, period_start, period_type, downloads, bytes_down, bytes_up
            """
        )
        .bindparams(uid=user_id, ps=period_start, pt=period_type, dl=add_downloads, bd=add_bytes)
    )
    result = await session.execute(stmt)
    row = result.first()
    assert row is not None
    return QuotaUsage(
        user_id=row.user_id,
        period_start=row.period_start,
        period_type=row.period_type,
        downloads=row.downloads,
        bytes_down=row.bytes_down,
        bytes_up=row.bytes_up,
    )


# ── Public API ──────────────────────────────────────────────────


async def get_snapshot(session: AsyncSession, user: User) -> QuotaSnapshot:
    """Build a read-only quota snapshot for *user*."""
    tier = effective_tier(user.tier, user.tier_expiry, is_admin=user.is_admin)
    policy = tier_policy(tier)

    today = _today_utc()
    month_start = _month_start_utc()

    daily = await session.get(QuotaUsage, (user.id, today, "daily"))
    monthly = await session.get(QuotaUsage, (user.id, month_start, "monthly"))

    # Count active (running) downloads for the user.
    from core.models import DownloadJob

    active = await session.scalar(
        select(func.count()).select_from(DownloadJob).where(
            DownloadJob.user_id == user.id,
            DownloadJob.status == "running",
        )
    )

    # Read-computed referral bonus (extra daily downloads).
    from core.referral import bonus_downloads

    bonus = await bonus_downloads(session, user.id) if not user.is_admin else 0

    return QuotaSnapshot(
        user_id=user.id,
        tier=tier,
        policy=policy,
        downloads_today=daily.downloads if daily else 0,
        bytes_today=daily.bytes_down if daily else 0,
        bytes_this_month=monthly.bytes_down if monthly else 0,
        active_downloads=active or 0,
        referral_bonus=bonus,
    )


async def check_quota(
    session: AsyncSession,
    user: User,
    *,
    file_size: int | None = None,
) -> QuotaSnapshot:
    """Verify that *user* can start a new download.

    Raises :class:`QuotaExceeded` when any dimension is exhausted.
    """
    snap = await get_snapshot(session, user)

    if user.is_admin:
        return snap

    # per-file size cap
    if file_size is not None and file_size > snap.policy.size_limit_bytes:
        raise QuotaExceeded(
            f"File size {file_size} exceeds tier limit {snap.policy.size_limit_bytes}",
            snap,
        )

    if not snap.can_download:
        raise QuotaExceeded("Daily download, bandwidth, or concurrent limit reached", snap)

    return snap


async def consume(
    session: AsyncSession,
    user: User,
    *,
    bytes_downloaded: int = 0,
) -> None:
    """Record a completed download: +1 download, +bytes in daily & monthly."""
    today = _today_utc()
    month_start = _month_start_utc()
    await _atomic_increment(
        session, user.id, today, "daily", add_downloads=1, add_bytes=bytes_downloaded
    )
    await _atomic_increment(
        session, user.id, month_start, "monthly", add_downloads=0, add_bytes=bytes_downloaded
    )


async def reset_daily(session: AsyncSession, user_id: int) -> int:
    """Zero out today's daily counter (admin override). Returns rows affected."""
    today = _today_utc()
    stmt = (
        text(
            """
            UPDATE quota_usage SET downloads = 0, bytes_down = 0
             WHERE user_id = :uid AND period_start = :ps AND period_type = 'daily'
            """
        )
        .bindparams(uid=user_id, ps=today)
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def reset_all(session: AsyncSession, user_id: int) -> int:
    """Zero out all quota counters for a user (admin nuclear reset)."""
    stmt = text("UPDATE quota_usage SET downloads = 0, bytes_down = 0 WHERE user_id = :uid").bindparams(
        uid=user_id
    )
    result = await session.execute(stmt)
    return result.rowcount or 0
