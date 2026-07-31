"""
SpideyBot — User Command Handlers.

Handles /start, /help, /dl, and default message detection.
"""

import re

import structlog
from telethon import events, Button
from telethon.errors import RPCError as TelethonRPCError

from spideybot import config
from spideybot import db
from spideybot.downloaders.terabox_downloader import TeraBoxDownloader

logger = structlog.get_logger(__name__)


def has_premium_access(user_id: int) -> bool:
    """Check if a user has premium access (admin or active premium status)."""
    return (user_id in config.ADMIN_IDS) or db.is_user_premium(user_id)


def extract_terabox_links(text: str) -> list:
    """Extract and validate potential TeraBox URLs from text."""
    if not text:
        return []
    url_pattern = re.compile(r'https?://[^\s]+', re.IGNORECASE)
    urls = url_pattern.findall(text)
    terabox_urls = []

    for url in urls:
        url_clean = url.rstrip('.,;!?)"\'')
        if config.is_terabox_url(url_clean):
            try:
                TeraBoxDownloader.parse_surl(url_clean)
                terabox_urls.append(url_clean)
            except (ValueError, RuntimeError):
                pass  # Intentional: non-TeraBox URLs silently skipped
    return terabox_urls


def register_user_handlers(bot, download_manager):
    """Register all user-facing command handlers on the given bot client.

    Args:
        bot: Telethon TelegramClient instance.
        download_manager: DownloadQueueManager instance for queuing downloads.
    """

    # ── Cancel Callback Handler ──────────────────────────────────────────────
    @bot.on(events.CallbackQuery(pattern=rb"cancel:(\d+)"))
    async def cancel_callback(event):
        """Handle inline cancel button presses."""
        entry_id = int(event.data_match.group(1))
        cancelled = await download_manager.cancel_task(entry_id)
        if cancelled:
            await event.answer("✅ Download cancelled.")
            try:
                await event.edit("❌ **SpideyBot:** Download cancelled.")
            except TelethonRPCError:
                pass  # Telegram edit race condition — safe to ignore
        else:
            await event.answer("⚠️ Task not found or already completed.", alert=True)

    @bot.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        """Send a welcome message when the command /start is issued."""
        user = await event.get_sender()
        user_id = event.sender_id
        username = user.username if user else None

        db.save_or_update_user(user_id, username)

        first_name = user.first_name if user else "there"
        welcome_text = (
            f"Hello {first_name}!\n\n"
            "I am SpideyBot, a multi-purpose assistant bot.\n"
            "My current features include a premium media downloader.\n\n"
            "Send me any TeraBox share link, or use the `/dl <link>` command for gallery-dl supported sites (YouTube, Twitter, Imgur, etc.).\n\n"
            "Use `/help` to see limits, premium status, and admin commands."
        )
        logger.info("User triggered /start", user_id=user_id)
        await event.respond(welcome_text)

    @bot.on(events.NewMessage(pattern='/help'))
    async def help_handler(event):
        """Send a help message detailing user tier limits."""
        user = await event.get_sender()
        user_id = event.sender_id
        username = user.username if user else None

        db.save_or_update_user(user_id, username)

        is_admin = user_id in config.ADMIN_IDS
        is_premium = has_premium_access(user_id)

        if is_admin:
            status_text = "👑 Admin Tier"
            limit_desc = "Unlimited download size, 5 concurrent downloads, priority queue processing"
        else:
            status_text = "✨ Premium Tier" if is_premium else "👤 Free Tier"
            limit_desc = "1GB max size, 5 concurrent downloads, priority queue processing" if is_premium else "100MB max size, 1 concurrent download"

        help_text = (
            "**SpideyBot Help & Information**\n\n"
            f"Your Membership: **{status_text}**\n"
            f"Limits: `{limit_desc}`\n\n"
            "**Commands:**\n"
            "• `/dl <link>` - Download files from TeraBox or any supported gallery-dl site (Twitter, Imgur, Reddit, etc.)\n"
            "• `/start` - Start the bot and get a welcome message\n"
            "• `/help` - Show this help page\n\n"
            "Just send or paste any valid TeraBox link, or use `/dl <link>` for any other site, and I'll add the task to the queue."
        )

        if is_admin:
            help_text += (
                "\n\n**Admin Commands:**\n"
                "• `/addpremium <username_or_userid> <days>` - Grant premium tier\n"
                "• `/removepremium <username_or_userid>` - Revoke premium tier\n"
                "• `/checkpremium <username_or_userid>` - Check user status"
            )

        logger.info("User triggered /help", user_id=user_id)
        await event.respond(help_text)

    @bot.on(events.NewMessage(pattern=r'/dl(?:\s+(https?://\S+))?'))
    async def dl_command_handler(event):
        """Queue a download request from a /dl command."""
        user = await event.get_sender()
        user_id = event.sender_id
        username = user.username if user else None

        db.save_or_update_user(user_id, username)

        link = event.pattern_match.group(1)
        if not link:
            await event.reply("⚠️ Please specify a valid URL.\nUsage: `/dl <link>`")
            return

        status_msg = await event.reply("⏳ **SpideyBot:** Queueing your download request...")

        is_admin = user_id in config.ADMIN_IDS
        is_premium = has_premium_access(user_id)

        status, task = await download_manager.add_task(user_id, event, link, is_premium, is_admin, status_msg)
        if status == "ok":
            pos = download_manager.get_queue_position(task.entry_id)
            await status_msg.edit(
                f"⏳ **SpideyBot:** Task queued (position #{pos} in queue)",
                buttons=[[Button.inline("❌ Cancel", data=f"cancel:{task.entry_id}")]]
            )

    @bot.on(events.NewMessage)
    async def default_message_handler(event):
        """Detect raw TeraBox links and remind user to use /dl."""
        if event.text and event.text.startswith('/'):
            return

        links = extract_terabox_links(event.text)
        if not links:
            return

        logger.info("Detected raw TeraBox link", user_id=event.sender_id)
        await event.reply(
            "⚠️ **To download, please use the `/dl` command.**\n"
            "Example: `/dl <terabox_link>`"
        )

    # ── Cancel Command ────────────────────────────────────────────────────────
    @bot.on(events.NewMessage(pattern=r'/cancel'))
    async def cancel_handler(event):
        """Cancel all active/queued downloads for the calling user."""
        user_id = event.sender_id
        cancelled_count = await download_manager.cancel_user_tasks(user_id)
        if cancelled_count > 0:
            await event.respond(f"❌ **SpideyBot:** Cancelled {cancelled_count} task(s).")
        else:
            await event.respond("ℹ️ **SpideyBot:** You have no active or queued tasks to cancel.")
