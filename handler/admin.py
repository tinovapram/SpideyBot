"""Admin command handlers: /addpremium, /removepremium, /checkpremium."""

from __future__ import annotations

import time

import structlog
from telethon import events

from core import config, db

logger = structlog.get_logger(__name__)


def register_admin_handlers(bot) -> None:
    @bot.on(events.NewMessage(pattern=r"/addpremium\s+(\S+)\s+(\d+)"))
    async def add_premium_handler(event):
        if not _is_admin(event.sender_id):
            await event.reply("❌ You are not authorized to use this command.")
            return

        target = event.pattern_match.group(1)
        days = int(event.pattern_match.group(2))

        if target.isdigit():
            expiry = db.add_premium_by_id(int(target), days)
            await event.reply(
                f"✅ **Premium Granted:**\n"
                f"• User ID: `{target}`\n• Duration: `{days}` days\n"
                f"• Expiry: `{_fmt(expiry)}`"
            )
        else:
            success, uid, expiry = db.add_premium_by_username(target, days)
            if success:
                await event.reply(
                    f"✅ **Premium Granted:**\n"
                    f"• Username: `{target}` (ID: `{uid}`)\n• Duration: `{days}` days\n"
                    f"• Expiry: `{_fmt(expiry)}`"
                )
            else:
                await event.reply(
                    f"❌ **User not found:** `{target}` is not in the bot's database.\n"
                    "They must interact with the bot at least once."
                )

    @bot.on(events.NewMessage(pattern=r"/removepremium\s+(\S+)"))
    async def remove_premium_handler(event):
        if not _is_admin(event.sender_id):
            await event.reply("❌ You are not authorized to use this command.")
            return

        target = event.pattern_match.group(1)
        success, message, uid = db.remove_premium(target)
        if success:
            await event.reply(f"✅ Successfully revoked premium status for `{target}` (ID: `{uid}`).")
        else:
            await event.reply(f"❌ Failed to revoke premium: {message}")

    @bot.on(events.NewMessage(pattern=r"/checkpremium\s+(\S+)"))
    async def check_premium_handler(event):
        if not _is_admin(event.sender_id):
            await event.reply("❌ You are not authorized to use this command.")
            return

        target = event.pattern_match.group(1)
        is_admin = target.isdigit() and int(target) in config.ADMIN_IDS
        if not is_admin and not target.isdigit():
            uid = db.find_user_by_username(target.lstrip("@"))
            is_admin = bool(uid and uid in config.ADMIN_IDS)

        if is_admin:
            await event.reply(f"👤 User: `{target}`\n✨ Status: Premium (Admin Bypass)")
        else:
            await event.reply(db.check_user_premium_status(target))


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def _fmt(timestamp: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
