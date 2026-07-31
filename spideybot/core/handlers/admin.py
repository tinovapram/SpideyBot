"""
SpideyBot — Admin Command Handlers.

Handles /addpremium, /removepremium, /checkpremium.
"""

import time

import structlog
from telethon import events

from spideybot import config
from spideybot import db

logger = structlog.get_logger(__name__)


def register_admin_handlers(bot):
    """Register all admin command handlers on the given bot client.

    Args:
        bot: Telethon TelegramClient instance.
    """

    @bot.on(events.NewMessage(pattern=r'/addpremium\s+(\S+)\s+(\d+)'))
    async def add_premium_handler(event):
        """Grant premium membership to a user for a number of days."""
        sender_id = event.sender_id
        if sender_id not in config.ADMIN_IDS:
            await event.reply("❌ You are not authorized to use this command.")
            return

        target = event.pattern_match.group(1)
        try:
            days = int(event.pattern_match.group(2))
        except ValueError:
            await event.reply("❌ Days must be a valid integer.")
            return

        if target.isdigit():
            target_uid = int(target)
            expiry = db.add_premium_by_id(target_uid, days)
            expiry_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry))
            await event.reply(
                f"✅ **Premium Granted:**\n"
                f"• User ID: `{target_uid}`\n"
                f"• Duration: `{days}` days\n"
                f"• Expiry: `{expiry_str}`"
            )
        else:
            success, uid, expiry = db.add_premium_by_username(target, days)
            if success:
                expiry_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry))
                await event.reply(
                    f"✅ **Premium Granted:**\n"
                    f"• Username: `{target}` (ID: `{uid}`)\n"
                    f"• Duration: `{days}` days\n"
                    f"• Expiry: `{expiry_str}`"
                )
            else:
                await event.reply(
                    f"❌ **User not found:** The username `{target}` is not in the bot's database.\n"
                    "They must start/interact with the bot at least once."
                )

    @bot.on(events.NewMessage(pattern=r'/removepremium\s+(\S+)'))
    async def remove_premium_handler(event):
        """Revoke premium membership from a user."""
        sender_id = event.sender_id
        if sender_id not in config.ADMIN_IDS:
            await event.reply("❌ You are not authorized to use this command.")
            return

        target = event.pattern_match.group(1)
        success, msg, uid = db.remove_premium(target)
        if success:
            await event.reply(f"✅ Successfully revoked premium status for `{target}` (ID: `{uid}`).")
        else:
            await event.reply(f"❌ Failed to revoke premium: {msg}")

    @bot.on(events.NewMessage(pattern=r'/checkpremium\s+(\S+)'))
    async def check_premium_handler(event):
        """Check membership status of any user."""
        sender_id = event.sender_id
        if sender_id not in config.ADMIN_IDS:
            await event.reply("❌ You are not authorized to use this command.")
            return

        target = event.pattern_match.group(1)

        is_target_admin = False
        if target.isdigit():
            if int(target) in config.ADMIN_IDS:
                is_target_admin = True
        else:
            uid = db.find_user_by_username(target.lstrip('@'))
            if uid and uid in config.ADMIN_IDS:
                is_target_admin = True

        if is_target_admin:
            await event.reply(f"👤 User: `{target}`\n✨ Status: Premium (Admin Bypass)")
        else:
            status_msg = db.check_user_premium_status(target)
            await event.reply(status_msg)
