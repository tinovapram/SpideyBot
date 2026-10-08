"""Users, onboarding state, and which limits actually apply to them.

Tier *policy* lives in :mod:`app.tiers` and the config table; this module is about the
stored per-user state that feeds it. Keeping the two apart is why ``tiers.resolve`` takes
plain values instead of a database row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import User
from app.tiers import EffectiveTier, is_admin, resolve


def _as_aware(moment: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC rather than rejecting it."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def upsert(
    db: AsyncSession,
    *,
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> User:
    """Register the user, or refresh the profile fields of an existing one.

    Only the profile fields are updated. Tier, expiry and onboarding state are left
    alone, so a repeat ``/start`` cannot silently reset a granted tier (FR-1.1).
    """
    statement = (
        pg_insert(User)
        .values(id=user_id, username=username, first_name=first_name)
        .on_conflict_do_update(
            index_elements=[User.id],
            set_={"username": username, "first_name": first_name},
        )
    )
    await db.execute(statement)

    user = await db.get(User, user_id)
    assert user is not None  # just inserted or updated, so it must exist
    # A Core statement writes around the ORM, so the identity map still holds the old
    # values; refreshing is what makes the returned object describe the database.
    await db.refresh(user)
    return user


async def fetch(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def ensure_registered(
    db: AsyncSession,
    *,
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> User:
    """The row for this user, creating it if needed. Convenience over :func:`upsert`."""
    existing = await fetch(db, user_id)
    if existing is not None:
        return existing
    return await upsert(db, user_id=user_id, username=username, first_name=first_name)


def tier_for(settings: Settings, user: User, *, now: datetime | None = None) -> EffectiveTier:
    """What this user actually gets right now.

    Admin comes from the configured id list or the stored flag, so an operator can be
    recognised either by setting an environment variable or by being marked in the
    database — and either way outranks any tier (spec 2.1).
    """
    return resolve(
        settings,
        tier=user.tier,
        tier_expiry=_as_aware(user.tier_expiry),
        is_admin=user.is_admin or is_admin(settings, user.id),
        now=now,
    )


async def mark_onboarded(db: AsyncSession, user: User, *, now: datetime | None = None) -> bool:
    """Record that onboarding completed; returns whether this was the first time.

    The return value is what decides full welcome vs the short already-started notice
    (FR-1.4/1.5), and storing the timestamp is what makes that state survive a restart
    (FR-1.6).
    """
    if user.onboarded_at is not None:
        return False

    user.onboarded_at = now or datetime.now(UTC)
    await db.flush()
    return True


async def grant_tier(
    db: AsyncSession,
    *,
    user_id: int,
    tier: str,
    expires_at: datetime | None = None,
    granted_by: int | None = None,
) -> User:
    """Set a user's tier, mirroring it onto the user row.

    The ``tier_grants`` row is the audit trail (who granted what, when, until when); the
    user row is what the hot paths read, so both are written here rather than leaving
    callers to remember.
    """
    from app.models import TierGrant

    user = await ensure_registered(db, user_id=user_id)
    user.tier = tier
    user.tier_expiry = expires_at
    db.add(
        TierGrant(
            user_id=user_id,
            tier=tier,
            granted_by=granted_by,
            expires_at=expires_at,
        )
    )
    await db.flush()
    return user


async def set_admin_flag(db: AsyncSession, *, user_id: int, value: bool) -> User:
    user = await ensure_registered(db, user_id=user_id)
    user.is_admin = value
    await db.flush()
    return user


async def username_of(db: AsyncSession, user_id: int) -> str | None:
    result = await db.execute(select(User.username).where(User.id == user_id))
    return result.scalar_one_or_none()
