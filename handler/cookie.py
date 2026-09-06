"""User cookie handlers: /setteracookie, /mycookie, /delcookie.

Lets a user paste their own TeraBox ``ndus`` cookie so they can download
from TeraBox using their own account (a free user's cookie also joins the
shared pool, per site policy).
"""

from __future__ import annotations

import structlog
from telethon import events

from core import cookies as cookie_store
from core.db import session_scope
from core.models import get_or_create_user
from core.tiers import DEFAULT_TIER, effective_tier

logger = structlog.get_logger(__name__)


def register_cookie_handlers(bot) -> None:
    @bot.on(events.NewMessage(pattern=r"/setteracookie(?:\s+(.+))?", incoming=True))
    async def set_cookie_handler(event):
        user_id = event.sender_id
        cookie = (event.pattern_match.group(1) or "").strip()

        if not cookie:
            await event.reply(
                "🔑 **Set your TeraBox cookie**\n\n"
                "Paste your TeraBox cookie after the command:\n"
                "`/setteracookie ndus=YOUR_VALUE; browserid=...`\n\n"
                "Your cookie is encrypted at rest and never logged."
            )
            return

        error = cookie_store.validate_cookie(cookie)
        if error is not None:
            await event.reply(f"❌ {error}")
            return

        try:
            async with session_scope() as session:
                user = await get_or_create_user(session, user_id, username=_sender_username(event))
                await cookie_store.save_cookie(session, user_id, cookie)
                tier = effective_tier(user.tier, user.tier_expiry)
        except cookie_store.CookieConfigError as exc:
            logger.error("Cookie storage misconfigured", error=str(exc))
            await event.reply("⚠️ Cookie storage is not configured. Contact an admin.")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to save cookie", error=str(exc))
            await event.reply("❌ Failed to save your cookie. Please try again.")
            return

        sharing = (
            "Your cookie also joins the shared pool (free tier) and may be "
            "used for other users' TeraBox downloads when the bot's accounts "
            "are busy."
            if tier == DEFAULT_TIER
            else "Your cookie is private to your account."
        )
        await event.reply(f"✅ TeraBox cookie saved.\n\n{sharing}")

    @bot.on(events.NewMessage(pattern=r"/mycookie", incoming=True))
    async def my_cookie_handler(event):
        user_id = event.sender_id
        try:
            async with session_scope() as session:
                exists = await cookie_store.has_cookie(session, user_id)
                cookie = await cookie_store.get_cookie(session, user_id) if exists else None
        except cookie_store.CookieConfigError:
            await event.reply("⚠️ Cookie storage is not configured. Contact an admin.")
            return

        if not cookie:
            await event.reply(
                "You have not set a TeraBox cookie yet.\n"
                "Use `/setteracookie ndus=...` to add one."
            )
            return

        # Show only a masked ndus for safety.
        await event.reply(
            "🔑 You have a TeraBox cookie saved.\n"
            f"• `ndus`: `{_mask_ndus(cookie)}`\n\n"
            "Use `/delcookie` to remove it."
        )

    @bot.on(events.NewMessage(pattern=r"/delcookie", incoming=True))
    async def del_cookie_handler(event):
        user_id = event.sender_id
        try:
            async with session_scope() as session:
                deleted = await cookie_store.delete_cookie(session, user_id)
        except cookie_store.CookieConfigError:
            await event.reply("⚠️ Cookie storage is not configured. Contact an admin.")
            return

        if deleted:
            await event.reply("🗑️ Your TeraBox cookie has been removed.")
        else:
            await event.reply("You don't have a TeraBox cookie saved.")


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
                return "•••"
            return f"{value[:4]}•••{value[-2:]}"
    return "•••"
