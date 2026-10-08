"""AC-10: `/quota` reflects real usage and the real queue.

Driven through the actual handler against a real database, because the point of the command is
that its numbers come from the same rows the gates read. A test that built a ``Usage`` by hand
would prove the copy renders and nothing about whether it is true.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import config, db, queue, quota, users
from app.models import ReferralBonus, User
from handlers.quota_info import quota_command
from tests.fakes.messages import FakeClient, FakeMessage, FakeUser

LINK = "https://example.test/album/1"
INVITEE_BASE = 9000


@pytest.fixture
def using(settings, monkeypatch, clean):
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


async def _quota(user_id: int, **profile) -> FakeMessage:
    message = FakeMessage(FakeUser(user_id, **profile), command=("quota",))
    await quota_command(FakeClient(), message)
    return message


async def _credits(referrer_id: int, count: int, *, valid_days: int = 30) -> None:
    """Referral credits, as credited invites leave them (FR-17.5).

    The invitees are real user rows because the column is a foreign key; that is also what
    stops a credit from being mintable for an arbitrary id.
    """
    async with db.session() as session:
        for index in range(count):
            invitee_id = INVITEE_BASE + index
            await users.upsert(session, user_id=invitee_id, first_name="Invitee")
            session.add(
                ReferralBonus(
                    referrer_id=referrer_id,
                    invitee_id=invitee_id,
                    expires_at=datetime.now(UTC) + timedelta(days=valid_days),
                )
            )


async def test_quota_reflects_stored_usage(using) -> None:
    async with db.session() as session:
        await users.upsert(session, user_id=1, username="a", first_name="A")
        for _ in range(2):
            await quota.reserve_download(session, 1, limit=10)

    message = await _quota(1, first_name="A")

    assert "Tier: 👤 Free" in message.reply
    assert "Downloads today: 2  — 8 left" in message.reply


async def test_quota_counts_a_waiting_job(using) -> None:
    """FR-10.6's distinction, as the user sees it: waiting is reported, not just 'active'."""
    async with db.session() as session:
        await users.upsert(session, user_id=1, first_name="A")
        await queue.enqueue(session, user_id=1, link=LINK, tier="free", priority=2.0)

    message = await _quota(1)

    assert "Queued: 1 waiting for a slot" in message.reply
    assert "Concurrent: 0/1 slots" in message.reply


async def test_quota_shows_referral_headroom(using) -> None:
    async with db.session() as session:
        await users.upsert(session, user_id=1, first_name="A")
    await _credits(1, 2)

    message = await _quota(1)

    assert "(+20 bonus)" in message.reply
    assert "— 30 left" in message.reply


async def test_quota_reflects_a_granted_tier(using) -> None:
    async with db.session() as session:
        await users.upsert(session, user_id=1, first_name="A")
        await users.grant_tier(session, user_id=1, tier="premium")

    message = await _quota(1)

    assert "Tier: ✨ Premium" in message.reply
    assert "Concurrent: 0/3 slots" in message.reply
    assert "Bandwidth today: 0 B / 100 GB" in message.reply
    assert "Link limit: ∞" in message.reply, "premium's size limit is unlimited (spec 13)"


async def test_an_exhausted_user_is_warned(using) -> None:
    async with db.session() as session:
        await users.upsert(session, user_id=1, first_name="A")
        for _ in range(10):
            await quota.reserve_download(session, 1, limit=10)

    message = await _quota(1)

    assert "0 left" in message.reply
    assert "**Quota reached**" in message.reply


async def test_the_command_registers_an_unknown_caller(using) -> None:
    """A user who never ran /start still gets an answer rather than an error."""
    message = await _quota(1234, first_name="New")

    assert "**Quota Status**" in message.reply
    async with db.session() as session:
        assert await session.get(User, 1234) is not None


async def test_expired_credits_stop_counting(using) -> None:
    """FR-17.5: a credit counts for 30 days, and no longer."""
    async with db.session() as session:
        await users.upsert(session, user_id=1, first_name="A")
    await _credits(1, 1, valid_days=-1)

    message = await _quota(1)

    assert "bonus" not in message.reply
    assert "— 10 left" in message.reply
