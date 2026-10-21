"""PostgreSQL-backed tests for quota, referrals and the shared cookie pool.

These exercise real SQL (statement shape, tier filtering, atomic counters), so
they need a live database and are skipped unless ``SPIDEY_TEST_DATABASE_URL``
is set.  Every test rolls its transaction back.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.cookies as cookies_module
from core import models
from core.config import get_settings
from core.cookies import save_cookie, shared_cookies
from core.quota import QuotaExceeded, consume, reserve_download
from core.referral import bonus_downloads, record_referral, try_credit_on_download
from core.tiers import tier_policy

_DB_URL = os.environ.get("SPIDEY_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not _DB_URL, reason="SPIDEY_TEST_DATABASE_URL is not set")

_TEST_USER_ID = 9_000_000_000_000_000_001


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield a session on a schema created on demand; roll back afterwards."""
    engine = create_async_engine(_DB_URL)
    async with engine.begin() as connection:
        await connection.run_sync(models.Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        try:
            yield db_session
        finally:
            await db_session.rollback()
    await engine.dispose()


async def _add_user(
    session: AsyncSession,
    *,
    user_id: int = _TEST_USER_ID,
    admin: bool = False,
) -> models.User:
    user = models.User(
        id=user_id,
        tier="premium" if admin else "free",
        is_admin=admin,
        timer_media_enabled=True,
        magic_word="WOW",
    )
    session.add(user)
    await session.flush()
    return user


async def test_reserve_download_stops_at_the_daily_limit(session: AsyncSession) -> None:
    user = await _add_user(session)
    daily_downloads = tier_policy("free").daily_downloads
    assert daily_downloads is not None

    for _ in range(daily_downloads):
        await reserve_download(session, user)

    with pytest.raises(QuotaExceeded, match="Daily download limit"):
        await reserve_download(session, user)


async def test_reserve_download_without_a_limit_never_blocks(session: AsyncSession) -> None:
    user = await _add_user(session, admin=True)

    for _ in range(3):
        snapshot = await reserve_download(session, user)

    assert snapshot.tier == "premium"


async def test_consume_records_bytes_and_blocks_bandwidth(session: AsyncSession) -> None:
    user = await _add_user(session)
    daily_bytes = tier_policy("free").daily_bytes
    assert daily_bytes is not None

    await reserve_download(session, user)
    await consume(session, user, bytes_downloaded=daily_bytes, reservation=True)

    with pytest.raises(QuotaExceeded, match="bandwidth"):
        await reserve_download(session, user)


async def test_consume_allows_unlimited_bandwidth_for_admins(session: AsyncSession) -> None:
    user = await _add_user(session, admin=True)

    await consume(session, user, bytes_downloaded=10**12)


async def test_referral_credit_unlocks_bonus_downloads(session: AsyncSession) -> None:
    referrer = await _add_user(session)
    invitee = await _add_user(session, user_id=_TEST_USER_ID + 1)

    assert await record_referral(session, referrer_id=referrer.id, invitee_id=invitee.id) is not None
    assert await bonus_downloads(session, referrer.id) == 0

    assert await try_credit_on_download(session, invitee.id) is True
    assert await try_credit_on_download(session, invitee.id) is False
    assert await bonus_downloads(session, referrer.id) == get_settings().referral_daily_bonus


async def test_referral_from_unknown_referrer_is_ignored(session: AsyncSession) -> None:
    invitee = await _add_user(session)

    unknown_referrer = _TEST_USER_ID + 99
    assert await record_referral(
        session, referrer_id=unknown_referrer, invitee_id=invitee.id
    ) is None


async def test_shared_cookies_exclude_admins_and_paid_tiers(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cookies_module, "_cipher", Fernet(Fernet.generate_key()))

    free_user = await _add_user(session)
    pro_user = await _add_user(session, user_id=_TEST_USER_ID + 1)
    pro_user.tier = "pro"
    expired_user = await _add_user(session, user_id=_TEST_USER_ID + 2)
    expired_user.tier = "pro"
    expired_user.tier_expiry = datetime.now(UTC) - timedelta(days=1)
    admin_user = await _add_user(session, user_id=_TEST_USER_ID + 3, admin=True)
    await session.flush()

    await save_cookie(session, free_user.id, "ndus=free")
    await save_cookie(session, pro_user.id, "ndus=pro")
    await save_cookie(session, expired_user.id, "ndus=expired")
    await save_cookie(session, admin_user.id, "ndus=admin")

    assert sorted(await shared_cookies(session)) == ["ndus=expired", "ndus=free"]
