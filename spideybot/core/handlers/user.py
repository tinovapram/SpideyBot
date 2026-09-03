"""
SpideyBot — User Command Handlers.

Handles /start, /stop, /help, /dl, /cancel, and default message detection.
"""

import re

import structlog
from telethon import events, Button
from telethon.errors import RPCError as TelethonRPCError

from spideybot import config
from spideybot import db
from spideybot import user_sessions
from spideybot.downloaders.terabox_downloader import TeraBoxDownloader
from spideybot.downloaders.telegram_msg_downloader import (
    download_tg_message,
    download_tg_range,
    is_tg_range_url,
    parse_tg_link,
)

logger = structlog.get_logger(__name__)


def has_premium_access(user_id: int) -> bool:
    """Check if a user has premium access (admin or active premium status)."""
    return (user_id in config.ADMIN_IDS) or db.is_user_premium(user_id)


def extract_terabox_links(text: str) -> list:
    """Extract and validate potential TeraBox URLs from text."""
    if not text:
        return []
    url_pattern = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    urls = url_pattern.findall(text)
    terabox_urls = []

    for url in urls:
        url_clean = url.rstrip(".,;!?)'\"")
        if config.is_terabox_url(url_clean):
            try:
                TeraBoxDownloader.parse_surl(url_clean)
                terabox_urls.append(url_clean)
            except (ValueError, RuntimeError):
                pass
    return terabox_urls


def _tier_label(user_id: int) -> str:
    """Human-readable tier badge."""
    if user_id in config.ADMIN_IDS:
        return "\U0001F451 Admin"
    if db.is_user_premium(user_id):
        return "\u2728 Premium"
    return "\U0001F464 Free"


def _session_badge(user_id: int) -> str:
    """One-liner showing whether the user session client is active."""
    if user_sessions.is_client_active(user_id):
        return "\U0001F534 Session active"
    if user_sessions.get_or_none(user_id):
        return "\U0001F7E1 Session saved (not running)"
    return "\U0001F7E2 No session"


def register_user_handlers(bot, download_manager):
    """Register all user-facing command handlers on the given bot client."""

    # ── Cancel Callback ────────────────────────────────────────────────────

    @bot.on(events.CallbackQuery(pattern=rb"cancel:(\d+)"))
    async def cancel_callback(event):
        """Handle inline cancel button presses."""
        entry_id = int(event.data_match.group(1))
        cancelled = await download_manager.cancel_task(entry_id)
        if cancelled:
            await event.answer("\u2705 Download cancelled.")
            try:
                await event.edit("\u274C **SpideyBot:** Download cancelled.")
            except TelethonRPCError:
                pass
        else:
            await event.answer("\u26A0\uFE0F Task not found or already completed.", alert=True)

    # ── /start ─────────────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern="/start"))
    async def start_handler(event):
        """Welcome the user and auto-start their session if available."""
        user = await event.get_sender()
        user_id = event.sender_id
        username = user.username if user else None

        db.save_or_update_user(user_id, username)

        first_name = user.first_name if user else "there"
        tier = _tier_label(user_id)
        session_info = _session_badge(user_id)

        # Try to start the user's session client
        session_started = False
        if user_sessions.get_or_none(user_id):
            session_started = await user_sessions.start_client(user_id)

        if session_started:
            session_line = "\U0001F534 **User account connected!**"
        elif user_sessions.get_or_none(user_id):
            session_line = "\u26A0\uFE0F Could not connect your session. Use /start again later."
        else:
            session_line = (
                "\U0001F510 Use `/login` to connect your Telegram account."
            )

        welcome_text = (
            f"\U0001F331 **Welcome, {first_name}!**\n\n"
            f"**Tier:** {tier}\n"
            f"**Session:** {session_info}\n\n"
            f"{session_line}\n\n"
            "I can download media from **TeraBox**, **YouTube**, **Twitter**, "
            "**Reddit**, **Imgur**, and [many more](https://github.com/mikf/gallery-dl-supported-sites).\n\n"
            "**Quick start:**\n"
            "  \u2022 Paste a link and I\u2019ll detect it automatically\n"
            "  \u2022 Or use `/dl <link>` for explicit control\n\n"
            "Type `/help` for the full command list."
        )
        logger.info("User triggered /start", user_id=user_id)
        await event.respond(welcome_text)

    # ── /stop ──────────────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern="/stop"))
    async def stop_handler(event):
        """Stop (disconnect) the user's account instance."""
        user_id = event.sender_id
        stopped = await user_sessions.stop_client(user_id)
        if stopped:
            await event.respond(
                "\U0001F534 **User account disconnected.**\n\n"
                "Your session file is still saved. Use `/start` to reconnect anytime."
            )
        else:
            if user_sessions.get_or_none(user_id):
                await event.respond(
                    "\u26A0\uFE0F Your session is saved but not currently running.\n"
                    "Use `/start` to connect it."
                )
            else:
                await event.respond(
                    "\U0001F7E2 You don\u2019t have a saved session.\n"
                    "Use `/login` to set one up."
                )
        logger.info("User triggered /stop", user_id=user_id, was_active=stopped)

    # ── /help ──────────────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern="/help"))
    async def help_handler(event):
        """Send a detailed help message."""
        user = await event.get_sender()
        user_id = event.sender_id
        username = user.username if user else None

        db.save_or_update_user(user_id, username)

        tier = _tier_label(user_id)
        session_info = _session_badge(user_id)

        if user_id in config.ADMIN_IDS:
            limits = "Unlimited size \u2022 5 concurrent \u2022 Priority queue"
        elif db.is_user_premium(user_id):
            limits = "1 GB max \u2022 5 concurrent \u2022 Priority queue"
        else:
            limits = "100 MB max \u2022 1 concurrent"

        help_text = (
            f"**\U0001F680 SpideyBot Help**\n\n"
            f"**Tier:** {tier}\n"
            f"**Limits:** {limits}\n"
            f"**Session:** {session_info}\n\n"
            "**\U0001F4E5 Download**\n"
            "  \u2022 `/dl <link>` \u2014 Download from TeraBox, YouTube, Twitter, Reddit, etc.\n"
            "  \u2022 `/dt <t.me link>` \u2014 Download media from a Telegram message\n"
            "  \u2022 `/dt <range>` \u2014 Download a range: `t.me/c/X/5-t.me/c/X/54`\n"
            "  \u2022 Paste any supported link and I\u2019ll auto-detect it\n"
            "  \u2022 `/cancel` \u2014 Cancel downloads (shows list if multiple)\n"
            "**\U0001F513 Account Session**\n"
            "  \u2022 `/login` \u2014 Login your Telegram account for private content\n"
            "  \u2022 `/logout` \u2014 Revoke your saved session\n"
            "  \u2022 `/start` \u2014 Start the bot (also connects your session if saved)\n"
            "  \u2022 `/stop` \u2014 Disconnect your session without deleting it\n\n"
            "**\U0001F4A1 Other**\n"
            "  \u2022 `/ping` \u2014 Test session anywhere (even in groups)\n"
            "  \u2022 `/help` \u2014 Show this message\n"
        )

        if user_id in config.ADMIN_IDS:
            help_text += (
                "\n**\U0001F451 Admin Commands**\n"
                "  \u2022 `/addpremium <user> <days>` \u2014 Grant premium\n"
                "  \u2022 `/removepremium <user>` \u2014 Revoke premium\n"
                "  \u2022 `/checkpremium <user>` \u2014 Check status\n"
            )

        logger.info("User triggered /help", user_id=user_id)
        await event.respond(help_text)

    # ── /dl ────────────────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern=r"/dl(?:\s+(https?://\S+))?"))
    async def dl_command_handler(event):
        """Queue a download request from a /dl command."""
        user = await event.get_sender()
        user_id = event.sender_id
        username = user.username if user else None

        db.save_or_update_user(user_id, username)

        link = event.pattern_match.group(1)
        if not link:
            await event.reply(
                "\u26A0\uFE0F Please specify a valid URL.\nUsage: `/dl <link>`"
            )
            return

        # ── If user session is active, let outgoing handler take over ──
        client = user_sessions.get_client(user_id)
        if client is not None:
            await event.reply(
                "\u2705 **Processing via your user account.**\n"
                "Queueing download… Use `/cancel` to abort."
            )
            # The outgoing /dl handler on the user's client will fire
            # for this same message and handle the actual download.
            logger.info("/dl delegated to user account", user_id=user_id)
            return

        # ── Fallback: process via bot ─────────────────────────────────────
        status_msg = await event.reply(
            "\u23F3 **SpideyBot:** Queueing your download request..."
        )

        is_admin = user_id in config.ADMIN_IDS
        is_premium = has_premium_access(user_id)

        status, task = await download_manager.add_task(
            user_id, event, link, is_premium, is_admin, status_msg
        )
        if status == "ok":
            pos = download_manager.get_queue_position(task.entry_id)
            await status_msg.edit(
                f"\u23F3 **SpideyBot:** Task queued (position #{pos} in queue)",
                buttons=[
                    [Button.inline(
                        "\u274C Cancel", data=f"cancel:{task.entry_id}"
                    )]
                ],
            )

    # ── /dt (download from Telegram message link) ──────────────────────────

    @bot.on(events.NewMessage(pattern=r"/dt(?:\s+(https?://\S+))?"))
    async def dt_command_handler(event):
        """Download media from a Telegram message link (single or range)."""
        user = await event.get_sender()
        user_id = event.sender_id
        username = user.username if user else None
        db.save_or_update_user(user_id, username)
        
        client = user_sessions.get_client(user_id)
        if client is not None:
            await event.reply(
                "\u2705 **Processing via your user account.**\n"
                "Queueing download… Use `/cancel` to abort."
            )
            logger.info("/dt delegated to user account", user_id=user_id)
            return
        else:
            await event.reply(
                "\u26A0\uFE0F **No user session active.**\n"
                "Use `/login` to connect your account for telegram content."
            )
            logger.info("/dt blocked (no session)", user_id=user_id)
            return
    # ── /cancel ────────────────────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern=r"/cancel(?:\s+(\d+))?"))
    async def cancel_handler(event):
        """Cancel active/queued downloads for the calling user.

        /cancel        — list tasks, then cancel all
        /cancel <id>   — cancel a specific task by entry ID
        """
        user_id = event.sender_id
        dm = download_manager

        client = user_sessions.get_client(user_id)
        if client is not None:
            return
        # /cancel <id> — cancel single task
        target_id = event.pattern_match.group(1)
        if target_id:
            entry_id = int(target_id)
            task = dm.active_tasks.get(entry_id)
            if task and task.user_id == user_id and not task.is_cancelled:
                task.cancel()
                await event.respond(
                    f"\u274C **SpideyBot:** Task #{entry_id} cancelled."
                )
            else:
                await event.respond(
                    "\u274C **SpideyBot:** Task not found or already completed."
                )
            return

        # /cancel — list tasks, then cancel all
        user_tasks = [
            t for t in dm.active_tasks.values()
            if t.user_id == user_id and not t.is_cancelled
        ]

        if not user_tasks:
            await event.respond(
                "\u2139\uFE0F **SpideyBot:** You have no active or queued tasks to cancel."
            )
            return

        if len(user_tasks) > 1:
            lines = ["**Your active tasks:**"]
            for t in user_tasks:
                pos = dm.get_queue_position(t.entry_id)
                status = f"queue #{pos}" if pos > 0 else "downloading"
                lines.append(f"  \u2022 `#{t.entry_id}` — {t.link[:50]}  ({status})")
            lines.append(
                "\nUse `/cancel <id>` to cancel one, "
                "or send `/cancel` again to cancel all."
            )
            await event.respond("\n".join(lines))
            return

        # Single task — cancel directly
        task = user_tasks[0]
        task.cancel()
        await event.respond(
            f"\u274C **SpideyBot:** Task #{task.entry_id} cancelled."
        )

    # ── Default message handler ────────────────────────────────────────────

    @bot.on(events.NewMessage)
    async def default_message_handler(event):
        """Detect raw TeraBox links and remind user to use /dl."""
        if event.text and event.text.startswith("/"):
            return
