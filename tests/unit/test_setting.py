"""Tests for handlers/setting.py — P7 inline preferences menu."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from handlers.setting import (
    back_main_callback,
    capture_input,
    delete_terabox_callback,
    reset_magic_callback,
    set_magic_callback,
    set_terabox_callback,
    setting_command,
    toggle_timer_callback,
)
from tests.fakes.messages import FakeCallbackQuery, FakeClient, FakeMessage, FakeUser

if TYPE_CHECKING:
    from pyrogram.types import InlineKeyboardMarkup


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def user() -> FakeUser:
    return FakeUser(12345, "TestUser")


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def mock_db() -> AsyncMock:
    """Mocked database session."""
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_prefs() -> dict[str, AsyncMock]:
    """Mocked preference functions."""
    return {
        "timer_media": AsyncMock(return_value=True),
        "terabox_cookie": AsyncMock(return_value=None),
        "magic_word": AsyncMock(return_value=None),
        "set_terabox_cookie": AsyncMock(),
        "set_timer_media": AsyncMock(),
        "set_magic_word": AsyncMock(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# /setting command
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setting_command_opens_menu(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """The /setting command opens the main settings menu."""
    message = FakeMessage(user, command=("setting",))

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
    ):
        await setting_command(client, message)

    assert len(message.replies) == 1
    reply = message.replies[0]
    assert "⚙️ **Settings**" in reply
    assert "🔑 **TeraBox Cookie:**" in reply
    assert "⏳ **TimerMedia:** ON" in reply
    assert "No cookie saved yet." in reply

    markup = message.reply_markups[0]
    assert markup is not None
    assert len(markup.inline_keyboard) == 3  # TeraBox, TimerMedia, Magic Word


@pytest.mark.asyncio
async def test_setting_command_displays_masked_cookie(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """When a cookie is saved, it is displayed masked."""
    message = FakeMessage(user, command=("setting",))
    mock_prefs["terabox_cookie"].return_value = "ndus12345678abcdefghXY"

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
    ):
        await setting_command(client, message)

    reply = message.replies[0]
    assert "ndus********XY" in reply
    assert "ndus12345678abcdefghXY" not in reply


@pytest.mark.asyncio
async def test_setting_command_timer_off(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """TimerMedia displays OFF when disabled."""
    message = FakeMessage(user, command=("setting",))
    mock_prefs["timer_media"].return_value = False

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
    ):
        await setting_command(client, message)

    reply = message.replies[0]
    assert "⏳ **TimerMedia:** OFF" in reply


# ─────────────────────────────────────────────────────────────────────────────
# TeraBox cookie flow
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_terabox_prompts_for_cookie(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """set_terabox callback enters awaiting-cookie state and prompts."""
    query = FakeCallbackQuery(user, "set_terabox")

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting._AWAITING", {}) as awaiting_dict,
    ):
        await set_terabox_callback(client, query)

    assert user.id in awaiting_dict
    assert awaiting_dict[user.id] == "cookie"
    assert query.message.edits
    edited_text = query.message.edits[-1]
    assert "🔑 **TeraBox Cookie Setup**" in edited_text
    assert "Send your cookie value" in edited_text


@pytest.mark.asyncio
async def test_capture_cookie_saves_and_returns_to_menu(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """Cookie input is saved and the main menu is displayed."""
    message = FakeMessage(user, text="ndus1234567890abcdefghXY")

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.set_terabox_cookie", mock_prefs["set_terabox_cookie"]),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
        patch("handlers.setting._AWAITING", {user.id: "cookie"}) as awaiting_dict,
    ):
        await capture_input(client, message)

    assert user.id not in awaiting_dict
    mock_prefs["set_terabox_cookie"].assert_called_once_with(mock_db, user.id, "ndus1234567890abcdefghXY")

    reply = message.replies[0]
    assert "✅ Cookie saved:" in reply
    assert "ndus********XY" in reply
    assert "⚙️ **Settings**" in reply


@pytest.mark.asyncio
async def test_delete_terabox_clears_cookie(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """delete_terabox callback clears the saved cookie."""
    query = FakeCallbackQuery(user, "delete_terabox")

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.set_terabox_cookie", mock_prefs["set_terabox_cookie"]),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
        patch("handlers.setting._AWAITING", {}),
    ):
        await delete_terabox_callback(client, query)

    mock_prefs["set_terabox_cookie"].assert_called_once_with(mock_db, user.id, None)
    assert query.message.edits
    edited_text = query.message.edits[-1]
    assert "⚙️ **Settings**" in edited_text
    assert query.answers
    assert "Cookie deleted" in query.answers[0]


# ─────────────────────────────────────────────────────────────────────────────
# TimerMedia toggle
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_toggle_timer_enables_when_disabled(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """toggle_timer enables TimerMedia when it was off."""
    query = FakeCallbackQuery(user, "toggle_timer")
    mock_prefs["timer_media"].return_value = False

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.set_timer_media", mock_prefs["set_timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
        patch("handlers.setting._AWAITING", {}),
    ):
        await toggle_timer_callback(client, query)

    mock_prefs["set_timer_media"].assert_called_once_with(mock_db, user.id, True)
    assert query.message.edits
    edited_text = query.message.edits[-1]
    assert "⏳ **TimerMedia:** ON" in edited_text
    assert query.answers
    assert "enabled" in query.answers[0].lower()


@pytest.mark.asyncio
async def test_toggle_timer_disables_when_enabled(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """toggle_timer disables TimerMedia when it was on."""
    query = FakeCallbackQuery(user, "toggle_timer")
    mock_prefs["timer_media"].return_value = True

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.set_timer_media", mock_prefs["set_timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
        patch("handlers.setting._AWAITING", {}),
    ):
        await toggle_timer_callback(client, query)

    mock_prefs["set_timer_media"].assert_called_once_with(mock_db, user.id, False)
    assert query.message.edits
    edited_text = query.message.edits[-1]
    assert "⏳ **TimerMedia:** OFF" in edited_text
    assert query.answers
    assert "disabled" in query.answers[0].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Magic word flow
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_magic_prompts_for_word(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """set_magic callback enters awaiting-magic state and prompts."""
    query = FakeCallbackQuery(user, "set_magic")
    mock_prefs["magic_word"].return_value = "ABRACADABRA"

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting._AWAITING", {}) as awaiting_dict,
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
        patch("handlers.setting.prefs.magic_word", mock_prefs["magic_word"]),
    ):
        await set_magic_callback(client, query)

    assert user.id in awaiting_dict
    assert awaiting_dict[user.id] == "magic"
    assert query.message.edits
    edited_text = query.message.edits[-1]
    assert "✨ **Magic Word**" in edited_text
    assert "Send a single word to set your magic word" in edited_text


@pytest.mark.asyncio
async def test_capture_magic_word_valid_input(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """Valid magic word is saved and menu returns."""
    message = FakeMessage(user, text="ABRACADABRA")

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.set_magic_word", mock_prefs["set_magic_word"]),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
        patch("handlers.setting._AWAITING", {user.id: "magic"}) as awaiting_dict,
    ):
        await capture_input(client, message)

    assert user.id not in awaiting_dict
    mock_prefs["set_magic_word"].assert_called_once_with(mock_db, user.id, "ABRACADABRA")

    reply = message.replies[0]
    assert "✅ Magic word set to:" in reply
    assert "ABRACADABRA" in reply
    assert "⚙️ **Settings**" in reply


@pytest.mark.asyncio
async def test_capture_magic_word_invalid_whitespace(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """Magic word with whitespace is rejected and state is retained."""
    message = FakeMessage(user, text="two words")

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.set_magic_word", mock_prefs["set_magic_word"]),
        patch("handlers.setting._AWAITING", {user.id: "magic"}) as awaiting_dict,
    ):
        await capture_input(client, message)

    assert user.id in awaiting_dict
    assert awaiting_dict[user.id] == "magic"
    mock_prefs["set_magic_word"].assert_not_called()

    reply = message.replies[0]
    assert "❌" in reply
    assert "single word" in reply.lower()
    assert "Try again" in reply


@pytest.mark.asyncio
async def test_capture_magic_word_invalid_too_long(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """Magic word over 64 characters is rejected."""
    message = FakeMessage(user, text="A" * 65)

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.set_magic_word", mock_prefs["set_magic_word"]),
        patch("handlers.setting._AWAITING", {user.id: "magic"}) as awaiting_dict,
    ):
        await capture_input(client, message)

    assert user.id in awaiting_dict
    assert awaiting_dict[user.id] == "magic"
    mock_prefs["set_magic_word"].assert_not_called()

    reply = message.replies[0]
    assert "❌" in reply
    assert "max 64 chars" in reply.lower()


@pytest.mark.asyncio
async def test_reset_magic_word(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """reset_magic callback resets the magic word to None (default WOW)."""
    query = FakeCallbackQuery(user, "reset_magic")

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.set_magic_word", mock_prefs["set_magic_word"]),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
        patch("handlers.setting._AWAITING", {}),
    ):
        await reset_magic_callback(client, query)

    mock_prefs["set_magic_word"].assert_called_once_with(mock_db, user.id, None)
    assert query.message.edits
    edited_text = query.message.edits[-1]
    assert "⚙️ **Settings**" in edited_text
    assert query.answers
    assert "reset to WOW" in query.answers[0]


# ─────────────────────────────────────────────────────────────────────────────
# Navigation and state clearing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_back_main_clears_awaiting_state(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """back_main callback clears any awaiting state and returns to menu."""
    query = FakeCallbackQuery(user, "back_main")

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.timer_media", mock_prefs["timer_media"]),
        patch("handlers.setting.prefs.terabox_cookie", mock_prefs["terabox_cookie"]),
        patch("handlers.setting.config.get_settings", return_value=MagicMock()),
        patch("handlers.setting._AWAITING", {user.id: "cookie"}) as awaiting_dict,
    ):
        await back_main_callback(client, query)

    assert user.id not in awaiting_dict
    assert query.message.edits
    edited_text = query.message.edits[-1]
    assert "⚙️ **Settings**" in edited_text


@pytest.mark.asyncio
async def test_command_aborts_input_capture(user: FakeUser, client: FakeClient, mock_db: AsyncMock, mock_prefs: dict[str, AsyncMock]) -> None:
    """Starting input with / aborts the conversation."""
    message = FakeMessage(user, text="/start")

    with (
        patch("handlers.setting.db_session", return_value=mock_db),
        patch("handlers.setting.prefs.set_terabox_cookie", mock_prefs["set_terabox_cookie"]),
        patch("handlers.setting._AWAITING", {user.id: "cookie"}) as awaiting_dict,
    ):
        await capture_input(client, message)

    # State cleared but no save happened
    assert user.id not in awaiting_dict
    mock_prefs["set_terabox_cookie"].assert_not_called()
    assert len(message.replies) == 0
