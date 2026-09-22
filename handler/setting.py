"""Unified /setting command with inline-button navigation.

Pressing a button triggers a ``CallbackQuery`` that edits the same
message in place — no chat clutter.

Current groups (add more by extending ``_GROUPS``):

- **tera** — TeraBox cookie: view / set / delete
"""

from __future__ import annotations

import structlog
from telethon import events
from telethon.tl.custom import Button

from core import cookies as cookie_store
from core.db import session_scope
from core.models import get_or_create_user
from core.tiers import DEFAULT_TIER, effective_tier

logger = structlog.get_logger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _sender_username(event) -> str | None:
    sender = getattr(event, "sender", None)
    return getattr(sender, "username", None) if sender else None


def _mask_ndus(cookie: str) -> str:
    """Return a masked ndus value (first 4 + last 2) for safe display."""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("ndus="):
            value = part[len("ndus="):]
            if len(value) <= 6:
                return "\u2022\u2022\u2022"
            return f"{value[:4]}\u2022\u2022\u2022{value[-2:]}"
    return "\u2022\u2022\u2022"


# ── Callback data constants ───────────────────────────────────────────────────
# Prefix bytes kept short (≤ 64 byte Telethon limit on callback data).

_CB_MAIN   = b"s:main"       # back to main menu
_CB_COOKIE = b"s:cookie"     # cookie status page
_CB_CSET   = b"s:cs"         # set cookie prompt
_CB_CSHOW  = b"s:cw"         # show masked cookie
_CB_CDEL   = b"s:cd"         # delete cookie
_CB_CDELY  = b"s:cdy"        # confirm delete


# ── Cookie helpers ────────────────────────────────────────────────────────────


async def _cookie_status_text(user_id: int) -> str:
    """Return the TeraBox cookie status paragraph."""
    try:
        async with session_scope() as session:
            exists = await cookie_store.has_cookie(session, user_id)
            cookie = (
                await cookie_store.get_cookie(session, user_id) if exists else None
            )
    except cookie_store.CookieConfigError:
        return "\u26a0\ufe0f Cookie storage is not configured. Contact an admin."
    except Exception:
        logger.exception("Failed to read cookie")
        return "\u274c Failed to read cookie status."

    if not cookie:
        return (
            "\U0001f511 **TeraBox Cookie**\n\n"
            "No cookie saved yet.\n"
            "Set one to download from TeraBox with your own account."
        )

    return (
        "\U0001f511 **TeraBox Cookie**\n\n"
        f"\u2022 `ndus`: `{_mask_ndus(cookie)}`\n\n"
        "Your cookie is encrypted at rest and never logged."
    )


# ── Page builders (text + buttons) ────────────────────────────────────────────


def _main_page() -> tuple[str, list[list[Button]]]:
    text = (
        "\u2699\ufe0f **Settings**\n\n"
        "Choose a category to manage:"
    )
    buttons = [[Button.inline("\U0001f511 TeraBox Cookie", data=_CB_COOKIE)]]
    return text, buttons


def _cookie_page(user_id: int):  # async-capable → returns a coroutine
    """Build (text, buttons) for the cookie status page."""

    async def _build():
        text = await _cookie_status_text(user_id)
        buttons = [
            [
                Button.inline("\U0001f4dd Set", data=_CB_CSET),
                Button.inline("\U0001f441\ufe0f View", data=_CB_CSHOW),
                Button.inline("\U0001f5d1\ufe0f Delete", data=_CB_CDELY),
            ],
            [Button.inline("\u2b05\ufe0f Back", data=_CB_MAIN)],
        ]
        return text, buttons

    return _build()


# ── CallbackQuery dispatcher ──────────────────────────────────────────────────


def register_setting_handlers(bot) -> None:
    """Register /setting command and its inline-button callbacks."""

    # ── /setting text command → show main menu ────────────────────────────
    @bot.on(events.NewMessage(pattern=r"/setting\s*$", incoming=True))
    async def setting_cmd(event):
        text, buttons = _main_page()
        await event.client.send_message(event.chat_id, text, buttons=buttons)

    # ── /setting tera set <cookie> — direct command fallback ──────────────
    @bot.on(events.NewMessage(pattern=r"/setting\s+tera\s+set\s+(.+)", incoming=True))
    async def setting_tera_set_cmd(event):
        cookie = event.pattern_match.group(1).strip()
        await _do_set_cookie(event, cookie)

    # ── All inline-button presses ─────────────────────────────────────────
    @bot.on(events.CallbackQuery)
    async def on_callback(event):
        data = event.data
        uid = event.sender_id

        # ── main menu ─────────────────────────────────────────────────────
        if data == _CB_MAIN:
            text, buttons = _main_page()
            await event.edit(text, buttons=buttons)
            return

        # ── cookie status page ────────────────────────────────────────────
        if data == _CB_COOKIE:
            text, buttons = await _cookie_page(uid)
            await event.edit(text, buttons=buttons)
            return

        # ── set cookie prompt ─────────────────────────────────────────────
        if data == _CB_CSET:
            text = (
                "\U0001f511 **Set TeraBox Cookie**\n\n"
                "Send your TeraBox cookie as your next message.\n"
                "Format: `ndus=YOUR_VALUE; browserid=...`\n\n"
                "_Or use:_ `/setting tera set ndus=...`"
            )
            await event.edit(text, buttons=[
                [Button.inline("\u2b05\ufe0f Back", data=_CB_COOKIE)]
            ])
            return

        # ── view masked cookie ────────────────────────────────────────────
        if data == _CB_CSHOW:
            try:
                async with session_scope() as session:
                    exists = await cookie_store.has_cookie(session, uid)
                    cookie = (
                        await cookie_store.get_cookie(session, uid)
                        if exists
                        else None
                    )
            except Exception:
                logger.exception("Failed to read cookie for view")
                await event.answer("\u274c Failed to read cookie.", alert=True)
                return

            if not cookie:
                await event.answer("No cookie saved yet.", alert=True)
                return

            text = (
                "\U0001f511 **Your TeraBox Cookie**\n\n"
                f"`{_mask_ndus(cookie)}`\n\n"
                "Encrypted at rest. Never logged."
            )
            await event.edit(text, buttons=[
                [Button.inline("\u2b05\ufe0f Back", data=_CB_COOKIE)]
            ])
            return

        # ── confirm delete ────────────────────────────────────────────────
        if data == _CB_CDELY:
            text = (
                "\U0001f5d1\ufe0f **Delete TeraBox Cookie?**\n\n"
                "This removes your saved cookie. You can set a new one anytime."
            )
            await event.edit(text, buttons=[
                [
                    Button.inline("\u274c Yes, delete", data=_CB_CDEL),
                    Button.inline("\u2b05\ufe0f Cancel", data=_CB_COOKIE),
                ],
            ])
            return

        # ── delete cookie ─────────────────────────────────────────────────
        if data == _CB_CDEL:
            try:
                async with session_scope() as session:
                    deleted = await cookie_store.delete_cookie(session, uid)
            except Exception:
                logger.exception("Failed to delete cookie")
                await event.answer("\u274c Failed to delete cookie.", alert=True)
                return

            if deleted:
                await event.answer("Cookie deleted.")
                text = (
                    "\U0001f5d1\ufe0f **Cookie deleted.**\n\n"
                    "Your TeraBox cookie has been removed."
                )
            else:
                await event.answer("Nothing to delete.")
                text = (
                    "\U0001f511 **TeraBox Cookie**\n\n"
                    "No cookie was saved."
                )

            await event.edit(text, buttons=[
                [Button.inline("\u2b05\ufe0f Back", data=_CB_COOKIE)]
            ])
            return


# ── Set-cookie logic (shared by button prompt and direct command) ─────────────


async def _do_set_cookie(event, cookie: str) -> None:
    error = cookie_store.validate_cookie(cookie)
    if error is not None:
        await event.client.send_message(event.chat_id, f"\u274c {error}")
        return

    try:
        async with session_scope() as session:
            user = await get_or_create_user(
                session, event.sender_id, username=_sender_username(event),
            )
            await cookie_store.save_cookie(session, event.sender_id, cookie)
            tier = effective_tier(user.tier, user.tier_expiry, is_admin=user.is_admin)
    except cookie_store.CookieConfigError:
        await event.client.send_message(event.chat_id, 
            "\u26a0\ufe0f Cookie storage is not configured. Contact an admin."
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save cookie", error=str(exc))
        await event.client.send_message(event.chat_id, "\u274c Failed to save your cookie. Please try again.")
        return

    sharing = (
        "Your cookie also joins the shared pool (free tier) and may be "
        "used for other users' TeraBox downloads when the bot's accounts "
        "are busy."
        if tier == DEFAULT_TIER
        else "Your cookie is private to your account."
    )
    await event.client.send_message(event.chat_id, 
        f"\u2705 TeraBox cookie saved.\n\n{sharing}",
        buttons=[[Button.inline("\U0001f511 View cookie", data=_CB_COOKIE)]],
    )
