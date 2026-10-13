"""``/setting`` — user preferences menu (FR-14, AC-12).

The menu is an inline-button interface that edits a message in-place on each interaction,
following the rule that navigation returns to the main menu after each action. Cookie status is
masked (first 4 + last 2 of `ndus`), and the cookie itself is stored locally.

Cookie and magic word inputs use a minimal conversation state (awaiting cookie/word) so the
next plain message is interpreted as the value to save.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import config, prefs
from app.db import session as db_session

log = logging.getLogger(__name__)

# Conversation state: which users are awaiting text input
_AWAITING: dict[int, Literal["cookie", "magic"]] = {}

# Main menu items
_BTN_TERABOX = InlineKeyboardButton("🔑 TeraBox Cookie", callback_data="set_terabox")
_BTN_TIMER = InlineKeyboardButton("⏳ TimerMedia", callback_data="toggle_timer")
_BTN_MAGIC = InlineKeyboardButton("✨ Magic Word", callback_data="set_magic")


def _cookie_display(cookie: str | None) -> str:
    """Show a stable-width cookie preview without exposing its hidden length."""
    if not cookie:
        return "No cookie saved yet."
    if len(cookie) <= 6:
        return "*" * len(cookie)
    return f"{cookie[:4]}{'*' * 8}{cookie[-2:]}"


async def _main_menu(
    db: Any,
    user_id: int,
    timer_enabled: bool | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if timer_enabled is None:
        timer_enabled = await prefs.timer_media(db, config.get_settings(), user_id)
    cookie = await prefs.terabox_cookie(db, user_id)
    cookie_display = _cookie_display(cookie)
    timer_status = "ON" if timer_enabled else "OFF"
    
    text = (
        "⚙️ **Settings**\n\n"
        f"🔑 **TeraBox Cookie:** `{cookie_display}`\n"
        f"⏳ **TimerMedia:** {timer_status}\n\n"
        "Choose a category to manage:"
    )
    
    return text, InlineKeyboardMarkup([
        [_BTN_TERABOX],
        [_BTN_TIMER],
        [_BTN_MAGIC]
    ])


@Client.on_message(filters.command("setting") & filters.private)
async def setting_command(client: Client, message: Message) -> None:
    sender = message.from_user
    if sender is None:
        return

    async with db_session() as db:
        text, markup = await _main_menu(db, sender.id)
        await message.reply_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex("^set_terabox$"))
async def set_terabox_callback(client: Client, query: CallbackQuery) -> None:
    """TeraBox cookie submenu: view/set/delete."""
    if query.from_user is None or query.message is None:
        return

    user_id = query.from_user.id
    _AWAITING[user_id] = "cookie"

    async with db_session() as db:
        cookie = await prefs.terabox_cookie(db, user_id)
        cookie_display = _cookie_display(cookie).replace("No cookie saved yet.", "None")

        text = (
            "🔑 **TeraBox Cookie Setup**\n\n"
            f"Current: `{cookie_display}`\n\n"
            "Send your cookie value to set it, or use the buttons below."
        )

        keyboard = [
            [InlineKeyboardButton("🗑️ Delete Cookie", callback_data="delete_terabox")],
            [InlineKeyboardButton("« Back", callback_data="back_main")]
        ]

        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        await query.answer()


@Client.on_callback_query(filters.regex("^delete_terabox$"))
async def delete_terabox_callback(client: Client, query: CallbackQuery) -> None:
    """Delete the stored TeraBox cookie."""
    if query.from_user is None or query.message is None:
        return

    user_id = query.from_user.id
    _AWAITING.pop(user_id, None)

    async with db_session() as db:
        await prefs.set_terabox_cookie(db, user_id, None)
        text, markup = await _main_menu(db, user_id)
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer("Cookie deleted", show_alert=False)


@Client.on_callback_query(filters.regex("^toggle_timer$"))
async def toggle_timer_callback(client: Client, query: CallbackQuery) -> None:
    """Toggle TimerMedia on/off."""
    if query.from_user is None or query.message is None:
        return

    settings = config.get_settings()
    async with db_session() as db:
        current = await prefs.timer_media(db, settings, query.from_user.id)
        new_value = not current
        await prefs.set_timer_media(db, query.from_user.id, new_value)

        text, markup = await _main_menu(db, query.from_user.id, timer_enabled=new_value)
        await query.message.edit_text(text, reply_markup=markup)
        status = "enabled" if new_value else "disabled"
        await query.answer(f"TimerMedia {status}", show_alert=False)


@Client.on_callback_query(filters.regex("^set_magic$"))
async def set_magic_callback(client: Client, query: CallbackQuery) -> None:
    """Magic word submenu: view/set/delete."""
    if query.from_user is None or query.message is None:
        return

    user_id = query.from_user.id
    _AWAITING[user_id] = "magic"

    async with db_session() as db:
        word = await prefs.magic_word(db, config.get_settings(), user_id)
        word_display = f"`{word}`" if word else "None (default: WOW)"

        text = (
            "✨ **Magic Word**\n\n"
            f"Current: {word_display}\n\n"
            "Send a single word to set your magic word, or use the buttons below."
        )

        keyboard = [
            [InlineKeyboardButton("🗑️ Reset to Default", callback_data="reset_magic")],
            [InlineKeyboardButton("« Back", callback_data="back_main")]
        ]

        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        await query.answer()


@Client.on_callback_query(filters.regex("^reset_magic$"))
async def reset_magic_callback(client: Client, query: CallbackQuery) -> None:
    """Reset magic word to default (NULL = WOW)."""
    if query.from_user is None or query.message is None:
        return

    user_id = query.from_user.id
    _AWAITING.pop(user_id, None)

    async with db_session() as db:
        await prefs.set_magic_word(db, user_id, None)
        text, markup = await _main_menu(db, user_id)
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer("Magic word reset to WOW", show_alert=False)


@Client.on_callback_query(filters.regex("^back_main$"))
async def back_main_callback(client: Client, query: CallbackQuery) -> None:
    """Return to settings main menu."""
    if query.from_user is None or query.message is None:
        return

    user_id = query.from_user.id
    _AWAITING.pop(user_id, None)

    async with db_session() as db:
        text, markup = await _main_menu(db, user_id)
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer()


def _awaiting_input_filter() -> filters.Filter:
    """Only messages from users awaiting text input."""
    async def func(_: Client, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in _AWAITING
    return filters.create(func)


@Client.on_message(filters.private & filters.text & _awaiting_input_filter())
async def capture_input(client: Client, message: Message) -> None:
    """Capture cookie or magic word input based on conversation state."""
    sender = message.from_user
    if sender is None or not message.text:
        return

    user_id = sender.id
    state = _AWAITING.pop(user_id, None)
    if state is None:
        return

    text_input = message.text.strip()

    # Commands abort the conversation
    if text_input.startswith("/"):
        return

    async with db_session() as db:
        if state == "cookie":
            await prefs.set_terabox_cookie(db, user_id, text_input)
            masked = _cookie_display(text_input)
            text, markup = await _main_menu(db, user_id)
            await message.reply_text(
                f"✅ Cookie saved: `{masked}`\n\n{text}",
                reply_markup=markup
            )

        elif state == "magic":
            # Validate: single word, no whitespace
            if " " in text_input or len(text_input) > 64:
                _AWAITING[user_id] = "magic"
                await message.reply_text(
                    "❌ Magic word must be a single word (max 64 chars). Try again:"
                )
                return

            await prefs.set_magic_word(db, user_id, text_input)
            text, markup = await _main_menu(db, user_id)
            await message.reply_text(
                f"✅ Magic word set to: `{text_input}`\n\n{text}",
                reply_markup=markup
            )
