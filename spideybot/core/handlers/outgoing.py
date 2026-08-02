"""
SpideyBot — Outgoing Message Handlers.

Detects commands typed by the user in *any* chat (including groups)
and replies using the user's own TelegramClient.

Currently handled:
  /ping  — test session connectivity
  /dl    — download media from any chat
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import structlog
from telethon import events
from telethon.errors import RPCError as TelethonRPCError

from spideybot import config, db, user_sessions

if TYPE_CHECKING:
    from spideybot.queue_manager import DownloadQueueManager

logger = structlog.get_logger(__name__)

# Set once during bot startup (see bot.py)
_download_manager: DownloadQueueManager | None = None


def set_download_manager(dm: DownloadQueueManager) -> None:
    """Store the download manager reference (called once from bot.py)."""
    global _download_manager
    _download_manager = dm


def register_outgoing_handlers(client, user_id: int) -> None:
    """Register all outgoing-command handlers on *client*.

    Called by ``user_sessions.start_client()`` for each user.
    """

    # ── /ping ──────────────────────────────────────────────────────

    @client.on(events.NewMessage(outgoing=True, pattern=r"/ping"))
    async def ping_handler(event):
        """Ping google.com and reply with latency via the user's account."""
        url = "https://www.google.com/generate_204"
        try:
            start = time.monotonic()
            async with aiohttp.ClientSession() as http:
                async with http.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    latency_ms = (time.monotonic() - start) * 1000
                    status = resp.status
            result = f"\u2705 **pong** \u2014 {latency_ms:.0f} ms (HTTP {status})"
        except Exception as e:
            result = f"\u274C **ping failed** \u2014 {type(e).__name__}: {e}"

        try:
            await event.respond(f"**/ping**\n{result}")
            logger.info("Outgoing /ping handled", user_id=user_id)
        except Exception as e:
            logger.error("Failed to send /ping reply", user_id=user_id, error=str(e))

    # ── /dl ────────────────────────────────────────────────────────

    @client.on(events.NewMessage(outgoing=True, pattern=re.compile(r"/dl", re.IGNORECASE)))
    async def dl_handler(event):
        """Detect ``/dl <link>`` in any chat and queue the download."""
        m = re.match(r"/dl(?:\s+(https?://\S+))?", event.raw_text or "", re.IGNORECASE)
        if not m:
            return

        link = m.group(1)
        if not link:
            try:
                await event.respond("\u26A0\uFE0F Please specify a URL.\nUsage: `/dl <link>`")
            except TelethonRPCError:
                pass
            return

        dm = _download_manager
        if dm is None:
            try:
                await event.respond("\u274C Bot is not ready yet.")
            except TelethonRPCError:
                pass
            return

        is_admin = user_id in config.ADMIN_IDS
        is_premium = has_premium_access(user_id)

        status_msg = await event.respond(
            "\u23F3 **SpideyBot:** Queueing your download request..."
        )

        status, task = await dm.add_task(
            user_id, event, link, is_premium, is_admin, status_msg
        )

        if status == "ok":
            pos = dm.get_queue_position(task.entry_id)
            try:
                await status_msg.edit(
                    f"\u23F3 **SpideyBot:** Task queued (position #{pos} in queue)"
                )
            except TelethonRPCError:
                pass

        logger.info("Outgoing /dl handled", user_id=user_id, link=link)

    # ── /cancel ────────────────────────────────────────────────────

    @client.on(events.NewMessage(outgoing=True, pattern=re.compile(r"/cancel(?:\s+(\d+))?", re.IGNORECASE)))
    async def cancel_handler(event):
        """Cancel active/queued downloads for the calling user.

        /cancel        — list tasks, then cancel all
        /cancel <id>   — cancel a specific task by entry ID
        """
        dm = _download_manager
        if dm is None:
            return

        m = re.match(r"/cancel(?:\s+(\d+))?", event.raw_text or "", re.IGNORECASE)
        target_id = m.group(1) if m else None

        # /cancel <id> — cancel single task
        if target_id:
            entry_id = int(target_id)
            task = dm.active_tasks.get(entry_id)
            if task and task.user_id == user_id and not task.is_cancelled:
                task.cancel()
                try:
                    await event.respond(f"\u274C Task #{entry_id} cancelled.")
                except TelethonRPCError:
                    pass
            else:
                try:
                    await event.respond("\u274C Task not found or already completed.")
                except TelethonRPCError:
                    pass
            return

        # /cancel — list tasks, then cancel all
        user_tasks = [
            t for t in dm.active_tasks.values()
            if t.user_id == user_id and not t.is_cancelled
        ]

        if not user_tasks:
            try:
                await event.respond(
                    "\u2139\uFE0F You have no active or queued tasks to cancel."
                )
            except TelethonRPCError:
                pass
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
            try:
                await event.respond("\n".join(lines))
            except TelethonRPCError:
                pass
            return

        # Single task — cancel directly
        task = user_tasks[0]
        task.cancel()
        try:
            await event.respond(f"\u274C Task #{task.entry_id} cancelled.")
        except TelethonRPCError:
            pass

        logger.info("Outgoing /cancel handled", user_id=user_id)


def has_premium_access(user_id: int) -> bool:
    """Check if a user has premium access (admin or active premium)."""
    return (user_id in config.ADMIN_IDS) or db.is_user_premium(user_id)
