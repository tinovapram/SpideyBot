"""Registration, onboarding state, and which limits actually apply (FR-1, FR-2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db, users
from app.models import TierGrant


async def test_a_new_user_starts_free_and_not_onboarded(session) -> None:
    user = await users.upsert(session, user_id=1, username="ada", first_name="Ada")

    assert user.tier == "free"
    assert user.tier_expiry is None
    assert user.onboarded_at is None


async def test_repeat_registration_refreshes_the_profile_only(session) -> None:
    """A repeat `/start` must not undo a granted tier or the onboarding state (FR-1.1)."""
    user = await users.upsert(session, user_id=1, username="old", first_name="Old")
    user.tier = "premium"
    user.onboarded_at = datetime.now(UTC)
    await session.flush()

    refreshed = await users.upsert(session, user_id=1, username="new", first_name="New")

    assert refreshed.username == "new"
    assert refreshed.first_name == "New"
    assert refreshed.tier == "premium"
    assert refreshed.onboarded_at is not None


async def test_onboarding_is_recorded_exactly_once(session) -> None:
    user = await users.upsert(session, user_id=1, first_name="Ada")

    first = await users.mark_onboarded(session, user)
    second = await users.mark_onboarded(session, user)

    assert (first, second) == (True, False)


async def test_onboarding_state_outlives_the_session(engine) -> None:
    """A new session stands in for a restart, which is what FR-1.6 requires."""
    async with db.session() as first:
        user = await users.upsert(first, user_id=7, first_name="Ada")
        assert await users.mark_onboarded(first, user) is True

    async with db.session() as second:
        user = await users.fetch(second, 7)
        assert user is not None
        assert await users.mark_onboarded(second, user) is False


# ── which limits apply ──────────────────────────────────────────────────


async def test_tier_follows_an_unexpired_grant(session, settings) -> None:
    user = await users.upsert(session, user_id=1, first_name="Ada")
    user.tier = "pro"
    user.tier_expiry = datetime.now(UTC) + timedelta(days=1)

    assert users.tier_for(settings, user).name == "pro"


async def test_an_expired_grant_reverts_to_free(session, settings) -> None:
    user = await users.upsert(session, user_id=1, first_name="Ada")
    user.tier = "pro"
    user.tier_expiry = datetime.now(UTC) - timedelta(days=1)

    tier = users.tier_for(settings, user)

    assert tier.name == "free"
    assert tier.limits == settings.tier.free


async def test_configured_admin_outranks_the_stored_tier(session, settings) -> None:
    admin_settings = settings.model_copy(update={"admin_ids": [5]})
    user = await users.upsert(session, user_id=5, first_name="Root")

    tier = users.tier_for(admin_settings, user)

    assert tier.is_admin
    assert tier.limits == admin_settings.tier.premium


async def test_a_database_admin_flag_also_counts(session, settings) -> None:
    """Either route to admin works, so an operator can use .env or the database."""
    await users.upsert(session, user_id=6, first_name="Root")
    user = await users.set_admin_flag(session, user_id=6, value=True)

    assert users.tier_for(settings, user).is_admin


async def test_granting_a_tier_records_the_audit_row(session) -> None:
    expires = datetime.now(UTC) + timedelta(days=30)

    user = await users.grant_tier(session, user_id=9, tier="pro", expires_at=expires, granted_by=1)

    assert user.tier == "pro"
    assert user.tier_expiry == expires

    # Queried rather than read through the relationship, which would lazy-load.
    result = await session.execute(select(TierGrant).where(TierGrant.user_id == 9))
    grants = result.scalars().all()
    assert len(grants) == 1
    assert grants[0].tier == "pro"
    assert grants[0].granted_by == 1
    assert grants[0].expires_at == expires
