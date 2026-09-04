"""
User command handlers: /start, /stop, /help, /site_list, /dl, /dt, /cancel.
"""

from __future__ import annotations

import re

import structlog
from telethon import Button, events
from telethon.errors import RPCError as TelethonRPCError

from core import config, db, sessions
from downloader.terabox import TeraBoxDownloader

logger = structlog.get_logger(__name__)


def has_premium_access(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS or db.is_user_premium(user_id)


def extract_terabox_links(text: str) -> list[str]:
    """Extract valid TeraBox URLs from free text."""
    if not text:
        return []
    urls = re.findall(r"https?://[^\s]+", text, re.IGNORECASE)
    result = []
    for url in urls:
        clean = url.rstrip(".,;!?)'\"")
        if not config.is_terabox_url(clean):
            continue
        try:
            TeraBoxDownloader.parse_surl(clean)
            result.append(clean)
        except Exception:
            pass
    return result


def _tier_label(user_id: int) -> str:
    if user_id in config.ADMIN_IDS:
        return "👑 Admin"
    if db.is_user_premium(user_id):
        return "✨ Premium"
    return "👤 Free"


def _session_badge(user_id: int) -> str:
    if sessions.is_client_active(user_id):
        return "🔴 Session active"
    if sessions.has_session(user_id):
        return "🟡 Session saved (not running)"
    return "🟢 No session"


_SITE_LIST = [
    ("YouTube", "youtube.com, youtu.be"),
    ("Twitter / X", "twitter.com, x.com"),
    ("TikTok", "tiktok.com"),
    ("Reddit", "reddit.com, redd.it"),
    ("Doodstream", "dood.stream, playmogo.com, ds2play.com"),
    ("Vidara", "vidara.me, vidaram.com"),
    ("Pinterest", "pinterest.com, pin.it"),
    ("Instagram", "instagram.com (gallery-dl)"),
    ("Spotify", "open.spotify.com"),
    ("SoundCloud", "soundcloud.com"),
    ("Bluesky", "bsky.app, bsky.social"),
    ("Threads", "threads.net"),
    ("LinkedIn", "linkedin.com, linkedin.cn"),
    ("Tumblr", "tumblr.com"),
    ("Snapchat", "snapchat.com"),
    ("Dailymotion", "dailymotion.com"),
    ("Streamtape", "streamtape.com"),
    ("CapCut", "capcut.com"),
    ("Douyin", "douyin.com"),
    ("Kuaishou", "kuaishou.com, ksplay.com"),
    ("TeraBox", "terabox.com, 1024tera.com, 4funbox.com"),
]


def register_user_handlers(bot, download_manager) -> None:
    """Register all user-facing handlers on *bot*."""

    @bot.on(events.CallbackQuery(pattern=rb"cancel:(\d+)"))
    async def cancel_callback(event):
        entry_id = int(event.data_match.group(1))
        if await download_manager.cancel_task(entry_id):
            await event.answer("✅ Download cancelled.")
            try:
                await event.edit("❌ **SpideyBot:** Download cancelled.")
            except TelethonRPCError:
                pass
        else:
            await event.answer("⚠️ Task not found or already completed.", alert=True)

    @bot.on(events.NewMessage(pattern="/start"))
    async def start_handler(event):
        user = await event.get_sender()
        user_id = event.sender_id
        db.save_or_update_user(user_id, user.username if user else None)

        first_name = user.first_name if user else "there"
        session_started = sessions.has_session(user_id) and await sessions.start_client(user_id)

        if session_started:
            session_line = "🔴 **User account connected!**"
        elif sessions.has_session(user_id):
            session_line = "⚠️ Could not connect your session. Use /start again later."
        else:
            session_line = "🔐 Use `/login` to connect your Telegram account."

        text = (
            f"🌱 **Welcome, {first_name}!**\n\n"
            f"**Tier:** {_tier_label(user_id)}\n"
            f"**Session:** {_session_badge(user_id)}\n\n"
            f"{session_line}\n\n"
            "**Supported sites:**\n"
            "TeraBox • YouTube • Twitter/X • TikTok • Reddit • Doodstream • Vidara • "
            "Pinterest • Instagram • Spotify • SoundCloud • Bluesky • Threads • LinkedIn • "
            "Tumblr • Snapchat • Dailymotion • Streamtape • [and 100+ more]"
            "(https://github.com/mikf/gallery-dl-supported-sites)\n\n"
            "**Quick start:**\n"
            "  • Paste a link and I'll auto-detect it\n"
            "  • Or use `/dl <link>` for explicit control\n\n"
            "Type `/help` for the full command list."
        )
        await event.respond(text)

    @bot.on(events.NewMessage(pattern="/stop"))
    async def stop_handler(event):
        user_id = event.sender_id
        stopped = await sessions.stop_client(user_id)
        if stopped:
            await event.respond("🔴 **Session disconnected.**\n\nYour session data is still saved.")
        elif sessions.has_session(user_id):
            await event.respond("⚠️ Session is saved but not running. Use `/start` to reconnect.")
        else:
            await event.respond("🟢 No saved session found. Use `/login` to connect your account.")

    @bot.on(events.NewMessage(pattern="/help"))
    async def help_handler(event):
        user = await event.get_sender()
        user_id = event.sender_id
        db.save_or_update_user(user_id, user.username if user else None)

        if user_id in config.ADMIN_IDS:
            limits = "Unlimited size • 5 concurrent • Priority queue"
        elif db.is_user_premium(user_id):
            limits = "1 GB max • 5 concurrent • Priority queue"
        else:
            limits = "100 MB max • 1 concurrent"

        text = (
            f"**🚀 SpideyBot Help**\n\n"
            f"**Tier:** {_tier_label(user_id)}  •  **Limits:** {limits}\n"
            f"**Session:** {_session_badge(user_id)}\n\n"
            "**📥 Downloading**\n"
            "  • `/dl <link>` — Download from any supported site\n"
            "  • Paste a link — auto-detected\n"
            "  • `/dt <t.me>` — Download from a Telegram message\n"
            "  • `/cancel` — Cancel downloads (or `/cancel <id>`)\n\n"
            "**🔑 Account Session**\n"
            "  • `/start` — Connect / welcome\n"
            "  • `/stop` — Disconnect session (saved)\n"
            "  • `/login` — Connect your Telegram account\n"
            "  • `/logout` — Remove saved session\n\n"
            "**📱 Supported Sites**\n"
            "  TeraBox • YouTube • Twitter/X • TikTok • Reddit • Doodstream • Vidara • "
            "Pinterest • Instagram • Spotify • SoundCloud • Bluesky • Threads • LinkedIn • "
            "Tumblr • Snapchat • Dailymotion • Streamtape • [100+ more]"
            "(https://github.com/mikf/gallery-dl-supported-sites)\n\n"
            "**💡 Other**\n"
            "  • `/ping` — Check bot status\n"
            "  • `/site_list` — Show all supported sites\n"
        )
        if user_id in config.ADMIN_IDS:
            text += (
                "\n**👑 Admin Commands**\n"
                "  • `/addpremium <user> <days>` — Grant premium\n"
                "  • `/removepremium <user>` — Revoke premium\n"
                "  • `/checkpremium <user>` — Check status\n"
            )
        await event.respond(text)

    @bot.on(events.NewMessage(pattern="/site_list"))
    async def site_list_handler(event):
        lines = [f"**📱 Supported Sites ({len(_SITE_LIST)})**\n"]
        lines += [f"  • **{name}** — `{domains}`" for name, domains in _SITE_LIST]
        lines.append(
            "\nPlus [100+ sites](https://github.com/mikf/gallery-dl-supported-sites) via gallery-dl."
        )
        await event.respond("\n".join(lines))

    @bot.on(events.NewMessage(pattern=r"/dl(?:\s+(https?://\S+))?"))
    async def dl_command_handler(event):
        user = await event.get_sender()
        user_id = event.sender_id
        db.save_or_update_user(user_id, user.username if user else None)

        link = event.pattern_match.group(1)
        if not link:
            await event.reply("⚠️ Please specify a valid URL.\nUsage: `/dl <link>`")
            return

        if sessions.get_client(user_id) is not None:
            await event.reply("✅ **Processing via your user account.** Queueing download…")
            return

        await _queue_download(event, user_id, link, download_manager)

    @bot.on(events.NewMessage(pattern=r"/dt(?:\s+(https?://\S+))?"))
    async def dt_command_handler(event):
        user = await event.get_sender()
        user_id = event.sender_id
        db.save_or_update_user(user_id, user.username if user else None)

        if sessions.get_client(user_id) is not None:
            await event.reply("✅ **Processing via your user account.** Queueing download…")
        else:
            await event.reply("⚠️ **No user session active.** Use `/login` to connect your account.")
        logger.info("/dt handled", user_id=user_id)

    @bot.on(events.NewMessage(pattern=r"/cancel(?:\s+(\d+))?"))
    async def cancel_handler(event):
        user_id = event.sender_id
        dm = download_manager

        if sessions.get_client(user_id) is not None:
            return

        target_id = event.pattern_match.group(1)
        if target_id:
            entry_id = int(target_id)
            task = dm.active_tasks.get(entry_id)
            if task and task.user_id == user_id and not task.is_cancelled:
                task.cancel()
                await event.respond(f"❌ **SpideyBot:** Task #{entry_id} cancelled.")
            else:
                await event.respond("❌ **SpideyBot:** Task not found or already completed.")
            return

        user_tasks = dm.user_tasks(user_id)
        if not user_tasks:
            await event.respond("ℹ️ **SpideyBot:** You have no active or queued tasks to cancel.")
            return

        if len(user_tasks) > 1:
            lines = ["**Your active tasks:**"]
            for task in user_tasks:
                pos = dm.get_queue_position(task.entry_id)
                status = f"queue #{pos}" if pos > 0 else "downloading"
                lines.append(f"  • `#{task.entry_id}` — {task.link[:50]}  ({status})")
            lines.append("\nUse `/cancel <id>` to cancel one, or send `/cancel` again to cancel all.")
            await event.respond("\n".join(lines))
            return

        task = user_tasks[0]
        task.cancel()
        await event.respond(f"❌ **SpideyBot:** Task #{task.entry_id} cancelled.")

    @bot.on(events.NewMessage)
    async def default_message_handler(event):
        text = event.text or ""
        if not text or text.startswith("/"):
            return

        links = extract_terabox_links(text)
        if not links:
            return

        user = await event.get_sender()
        user_id = event.sender_id
        db.save_or_update_user(user_id, user.username if user else None)
        await _queue_download(event, user_id, links[0], download_manager)


async def _queue_download(event, user_id: int, link: str, manager) -> None:
    is_admin = user_id in config.ADMIN_IDS
    is_premium = has_premium_access(user_id)

    status_msg = await event.reply("⏳ **SpideyBot:** Queueing your download request...")
    status, task = await manager.add_task(user_id, event, link, is_premium, is_admin, status_msg)

    if status == "ok":
        pos = manager.get_queue_position(task.entry_id)
        await status_msg.edit(
            f"⏳ **SpideyBot:** Task queued (position #{pos} in queue)",
            buttons=[[Button.inline("❌ Cancel", data=f"cancel:{task.entry_id}")]],
        )
