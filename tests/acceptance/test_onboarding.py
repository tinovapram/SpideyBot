"""AC-1 onboarding, driven through the real handlers against a real database.

The handler is called directly with a fake message, so everything except Telegram's own
delivery is exercised: registration, referral capture, the first-vs-repeat branch, and the
copy the user actually receives. Assertions read through fresh sessions, because the
handler commits its own work.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import config, db, users
from app.models import Referral, User
from handlers.help import help_command
from handlers.start import start_command
from tests.fakes.messages import FakeClient, FakeMessage, FakeUser


@pytest.fixture
def using(settings, monkeypatch, clean):
    """Test settings, on an emptied database.

    Depends on ``clean`` so every case here starts from nothing, and on ``settings`` so the
    handlers never read the developer's real config.
    """
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


async def _stored_user(user_id: int) -> User | None:
    async with db.session() as session:
        return await session.get(User, user_id)


async def _referrals() -> list[Referral]:
    async with db.session() as session:
        result = await session.execute(select(Referral))
        return list(result.scalars().all())


async def _start(user_id: int, *, command: tuple[str, ...] = ("start",), **profile):
    message = FakeMessage(FakeUser(user_id, **profile), command=command)
    await start_command(FakeClient(), message)
    return message


# ── registration and the two onboarding paths ───────────────────────────


async def test_a_new_user_is_registered_and_welcomed(using) -> None:
    message = await _start(1001, first_name="Ada", username="ada")

    assert "**Welcome to SpideyBot**" in message.reply
    assert "**Welcome, Ada!**" in message.reply
    assert "Tier: 👤 Free" in message.reply
    assert "**Essential commands**" in message.reply

    user = await _stored_user(1001)
    assert user is not None
    assert user.username == "ada"
    assert user.tier == "free"
    assert user.onboarded_at is not None, "onboarding must be recorded on the first /start"


async def test_a_repeat_start_gets_the_short_notice(using) -> None:
    await _start(1001, first_name="Ada")

    second = await _start(1001, first_name="Ada")

    assert "Already started" in second.reply
    assert "**Welcome to SpideyBot**" not in second.reply
    assert "**Essential commands**" not in second.reply


async def test_the_notice_still_reports_the_session_state(using) -> None:
    """FR-1.5: brief, but it must still say whether the session is live."""
    await _start(1001)
    second = await _start(1001)

    assert "Sessions: 🔴 No session" in second.reply


async def test_onboarding_state_survives_a_new_session(using) -> None:
    """Standing in for a restart, which is what FR-1.6 requires."""
    await _start(1001)

    assert (await _stored_user(1001)).onboarded_at is not None
    third = await _start(1001)

    assert "Already started" in third.reply


async def test_the_welcome_carries_the_upgrade_button_once_a_url_is_set(
    settings, using, monkeypatch
) -> None:
    ux = settings.ux.model_copy(update={"upgrade_url": "https://example.invalid/upgrade"})
    monkeypatch.setattr(config, "get_settings", lambda: settings.model_copy(update={"ux": ux}))

    first = await _start(1001)
    second = await _start(1001)

    assert first.reply_markups[0] is not None
    # The repeat notice stays button-free, because it is meant to be brief.
    assert second.reply_markups[0] is None


# ── referral capture (FR-1.2) ───────────────────────────────────────────


async def test_an_invite_records_a_pending_referral(using) -> None:
    await _start(2002, first_name="Referrer")

    await _start(1001, command=("start", "2002"), first_name="Invitee")

    referrals = await _referrals()
    assert len(referrals) == 1
    assert referrals[0].referrer_id == 2002
    assert referrals[0].invitee_id == 1001
    assert referrals[0].status == "pending", "crediting waits for a first download (FR-17.4)"

    invitee = await _stored_user(1001)
    assert invitee.referred_by == 2002


async def test_capture_never_changes_the_message(using) -> None:
    """FR-1.2 makes the step silent, so the invitee still sees a normal welcome."""
    await _start(2002)

    invited = await _start(1001, command=("start", "2002"), first_name="Ada")

    assert "**Welcome to SpideyBot**" in invited.reply
    assert "**Welcome, Ada!**" in invited.reply


async def test_a_self_referral_records_nothing(using) -> None:
    await _start(1001, command=("start", "1001"))

    assert await _referrals() == []


async def test_the_first_referrer_wins(using) -> None:
    await _start(2002, first_name="First")
    await _start(3003, first_name="Second")
    await _start(1001, command=("start", "2002"))

    await _start(1001, command=("start", "3003"))

    referrals = await _referrals()
    assert len(referrals) == 1
    assert referrals[0].referrer_id == 2002


async def test_a_referrer_who_never_started_is_ignored(using) -> None:
    """Otherwise a typed payload could mint user rows for arbitrary ids."""
    await _start(1001, command=("start", "999999"))

    assert await _referrals() == []
    assert await _stored_user(999999) is None


async def test_a_junk_payload_is_ignored_without_error(using) -> None:
    message = await _start(1001, command=("start", "not-a-number"))

    assert "**Welcome to SpideyBot**" in message.reply
    assert await _referrals() == []


# ── /help reflects the caller (FR-2) ────────────────────────────────────


async def test_help_shows_the_callers_own_limits(settings, using) -> None:
    async with db.session() as session:
        await users.grant_tier(session, user_id=1003, tier="pro")
    message = FakeMessage(FakeUser(1003))

    await help_command(FakeClient(), message)

    assert "Tier: ✨ Pro" in message.reply
    assert "Link limit: 10 GB" in message.reply
    assert "**Essential commands**" in message.reply


async def test_help_for_an_unregistered_user_falls_back_to_defaults(using) -> None:
    """Someone who has never run /start still deserves a correct answer."""
    message = FakeMessage(FakeUser(1004))

    await help_command(FakeClient(), message)

    assert "Tier: 👤 Free" in message.reply
    assert "Link limit: 500 MB" in message.reply


async def test_help_shows_admin_commands_to_an_operator(settings, using, monkeypatch) -> None:
    admin_settings = settings.model_copy(update={"admin_ids": [1005]})
    monkeypatch.setattr(config, "get_settings", lambda: admin_settings)
    message = FakeMessage(FakeUser(1005))

    await help_command(FakeClient(), message)

    assert "**Admin only**" in message.reply
