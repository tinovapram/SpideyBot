"""The two commands, driven directly.

Kurigram delivers updates over a socket, so exercising a handler through the library
would need a live user. Calling the handler coroutine with a stub message covers
everything up to delivery — the filters, settings lookup, tier resolution and the
catalog rendering — and leaves only Telegram's routing unverified.
"""

from __future__ import annotations

import pytest
from pyrogram.types import InlineKeyboardMarkup

from app import catalog, config, paths
from app.config import build_settings
from handlers.help import help_command
from handlers.start import start_command, upgrade_keyboard


class FakeUser:
    def __init__(self, user_id: int, first_name: str = "Ada") -> None:
        self.id = user_id
        self.first_name = first_name


class FakeMessage:
    """Just enough of `pyrogram.types.Message` for these handlers."""

    def __init__(self, user: FakeUser | None) -> None:
        self.from_user = user
        self.replies: list[str] = []
        self.reply_markups: list[object] = []

    async def reply_text(self, text: str, reply_markup: object = None, **_kwargs: object) -> None:
        self.replies.append(text)
        self.reply_markups.append(reply_markup)


@pytest.fixture
def settings(fake_secrets: dict[str, str]):
    return build_settings(**fake_secrets)


@pytest.fixture
def using(settings, monkeypatch):
    """Point the handlers at test settings instead of the developer's real config."""

    def _switch(active=settings):
        monkeypatch.setattr(config, "get_settings", lambda: active)
        return active

    return _switch


# ── /start ──────────────────────────────────────────────────────────────


async def test_start_sends_the_welcome_and_this_users_tier(using) -> None:
    using()
    message = FakeMessage(FakeUser(1))

    await start_command(None, message)

    text = message.replies[0]
    assert catalog.WELCOME in text
    assert "**Essential commands**" in text
    assert "**Welcome, Ada!**" in text
    assert "Tier: 👤 Free" in text


async def test_start_reports_no_session_for_a_fresh_user(using) -> None:
    using()
    message = FakeMessage(FakeUser(1))

    await start_command(None, message)

    assert f"Sessions: {catalog.NO_SESSION}" in message.replies[0]


async def test_start_detects_an_existing_session_file(using, tmp_path, monkeypatch) -> None:
    using()
    monkeypatch.setattr(paths, "SESSIONS_DIR", tmp_path)
    (tmp_path / "7.session").write_text("not really a session")
    message = FakeMessage(FakeUser(7))

    await start_command(None, message)

    # Saved, but nothing reconnects it yet, so it is not reported as live.
    assert f"Sessions: {catalog.SESSION_SAVED}" in message.replies[0]


async def test_start_uses_the_configured_magic_word(settings, using) -> None:
    ux = settings.ux.model_copy(update={"default_magic_word": "POW"})
    using(settings.model_copy(update={"ux": ux}))
    message = FakeMessage(FakeUser(1))

    await start_command(None, message)

    assert "`POW`" in message.replies[0]


async def test_start_gives_an_operator_admin_commands(settings, using) -> None:
    using(settings.model_copy(update={"admin_ids": [42]}))
    message = FakeMessage(FakeUser(42))

    await start_command(None, message)

    text = message.replies[0]
    assert "**Admin only**" in text
    assert "Tier: 👑 Admin" in text
    assert catalog.FREE_TIER_NOTE not in text


async def test_start_survives_a_message_without_a_sender(using) -> None:
    """Service messages and channel posts have no `from_user`."""
    using()
    message = FakeMessage(None)

    await start_command(None, message)

    assert "**Welcome, there!**" in message.replies[0]


async def test_start_replies_exactly_once(using) -> None:
    """The spec replaces the progress message later; onboarding must not spam."""
    using()
    message = FakeMessage(FakeUser(1))

    await start_command(None, message)

    assert len(message.replies) == 1


# ── the Upgrade button (AC-1, plan D29) ─────────────────────────────────


def test_upgrade_keyboard_is_absent_without_a_url() -> None:
    """No destination is specified anywhere in the spec, so nothing is invented."""
    assert upgrade_keyboard("") is None


def test_upgrade_keyboard_builds_a_labelled_url_button() -> None:
    markup = upgrade_keyboard("https://example.invalid/upgrade")

    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert button.url == "https://example.invalid/upgrade"
    assert button.text


async def test_start_attaches_the_upgrade_button_when_a_url_is_set(settings, using) -> None:
    ux = settings.ux.model_copy(update={"upgrade_url": "https://example.invalid/upgrade"})
    using(settings.model_copy(update={"ux": ux}))
    message = FakeMessage(FakeUser(1))

    await start_command(None, message)

    markup = message.reply_markups[0]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].url == "https://example.invalid/upgrade"


async def test_start_omits_the_button_when_no_url_is_set(settings, using) -> None:
    """No destination is specified anywhere in the spec, so nothing is invented."""
    using(settings)
    message = FakeMessage(FakeUser(1))

    await start_command(None, message)

    assert settings.ux.upgrade_url == ""
    assert message.reply_markups[0] is None


# ── /help ───────────────────────────────────────────────────────────────


async def test_help_sends_the_command_list(using) -> None:
    using()
    message = FakeMessage(FakeUser(1))

    await help_command(None, message)

    text = message.replies[0]
    assert "**Essential commands**" in text
    assert "/dl" in text
    assert "**Welcome" not in text  # help is not onboarding


async def test_help_hides_admin_commands_from_normal_users(using) -> None:
    using()
    message = FakeMessage(FakeUser(1))

    await help_command(None, message)

    assert "Admin only" not in message.replies[0]


async def test_help_shows_admin_commands_to_an_operator(settings, using) -> None:
    using(settings.model_copy(update={"admin_ids": [42]}))
    message = FakeMessage(FakeUser(42))

    await help_command(None, message)

    assert "**Admin only**" in message.replies[0]
