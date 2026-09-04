"""
Outgoing command handlers.

Detect commands typed by the user in *any* chat (groups, channels) and reply
using the user's own TelegramClient: /ping, /dl, /dt, /cancel.
"""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING

import aiohttp
import structlog
from telethon import events
from telethon.errors import RPCError as TelethonRPCError

from core import config, db
from downloader.telegram import (
    download_tg_message,
    download_tg_range,
    is_tg_range_url,
    parse_tg_link,
    parse_tg_range,
)
from utils import paths
from utils.files import prepare_media
from utils.progress import StatusMessage

if TYPE_CHECKING:
    from core.queue import DownloadQueueManager

logger = structlog.get_logger(__name__)

_download_manager: "DownloadQueueManager | None" = None


def set_download_manager(manager: "DownloadQueueManager") -> None:
    global _download_manager
    _download_manager = manager


def has_premium_access(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS or db.is_user_premium(user_id)


def _determine_tg_flags(meta: dict | None) -> dict:
    if not meta:
        return {}
    if meta.get("photo"):
        return {"as_image": True}
    if meta.get("animated"):
        return {"supports_streaming": True, "nosound_video": True}
    if meta.get("video"):
        return {"supports_streaming": True}
    if meta.get("force_document"):
        return {"force_document": True}
    return {}


def register_outgoing_handlers(client, user_id: int) -> None:
    # ── /ping ────────────────────────────────────────────────────

    @client.on(events.NewMessage(outgoing=True, pattern=r"/ping"))
    async def ping_handler(event):
        lines = []

        try:
            me = await client.get_me()
            lines.append(f"✅ **Session** — logged in as **{me.first_name}** (id: {me.id})")
        except Exception as exc:
            lines.append(f"❌ **Session** — not authorized: {exc}")

        try:
            start = time.monotonic()
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    "https://www.google.com/generate_204", timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    latency = (time.monotonic() - start) * 1000
                    lines.append(f"✅ **Internet** — {latency:.0f} ms (HTTP {resp.status})")
        except Exception as exc:
            lines.append(f"❌ **Internet** — {type(exc).__name__}: {exc}")

        try:
            await client.send_message(
                event.chat_id, "**/ping**\n" + "\n".join(lines), reply_to=event.message
            )
        except Exception as exc:
            logger.error("Failed to send /ping reply", user_id=user_id, error=str(exc))

    # ── /dt (Telegram message link) ─────────────────────────────

    @client.on(events.NewMessage(outgoing=True, pattern=re.compile(r"/dt", re.IGNORECASE)))
    async def dt_handler(event):
        match = re.match(r"/dt(?:\s+(https?://\S+))?", event.raw_text or "", re.IGNORECASE)
        if not match:
            return

        link = match.group(1)
        if not link:
            await _reply(client, event, "⚠️ Please specify a Telegram message link.\nUsage: `/dt <t.me link>`")
            return

        is_range = is_tg_range_url(link)
        try:
            parse_tg_range(link) if is_range else parse_tg_link(link)
        except ValueError as exc:
            await _reply(client, event, f"❌ {exc}")
            return

        label = " (range)" if is_range else ""
        status_msg = await client.send_message(
            event.chat_id, f"⏳ **SpideyBot:** Downloading from Telegram{label}...", reply_to=event.message
        )
        status = StatusMessage(status_msg)
        status.set_header(f"⬇️ **SpideyBot:** Downloading from Telegram{label}")

        output_dir = str(paths.DOWNLOADS_DIR / f"tg_{user_id}")
        dl_cb = status.bytes_cb("tgdl", "⬇️", "Downloading")
        try:
            if is_range:
                result = await download_tg_range(client, link, output_dir=output_dir, progress_callback=dl_cb)
            else:
                result = await download_tg_message(client, link, output_dir=output_dir, progress_callback=dl_cb)
        except Exception as exc:
            logger.error("TG download failed", error=str(exc))
            result = {"ok": False, "error": str(exc)}

        if not result["ok"]:
            await status.close(f"❌ **SpideyBot:** {result['error']}")
            return

        files = result["files"]
        metadata = result.get("file_metadata") or [None] * len(files)

        try:
            status.drop("tgdl")
            status.set_header("📤 **SpideyBot:** Uploading to Telegram")
            up_cb = status.bytes_cb("ul", "📤", "Uploading")
            media = []
            for fp, meta in zip(files, metadata):
                if not os.path.isfile(fp):
                    continue
                try:
                    media.append(
                        await prepare_media(client, fp, progress_callback=up_cb, **_determine_tg_flags(meta))
                    )
                except Exception as exc:
                    logger.warning("Failed to prepare TG file", file=fp, error=str(exc))

            if media:
                await client.send_file(
                    event.chat_id, media,
                    caption=f"✅ **SpideyBot:** Downloaded {len(media)} file(s) from Telegram{label}.",
                    reply_to=event.message,
                )
            else:
                await client.send_message(
                    event.chat_id, "✅ **SpideyBot:** Downloaded files but nothing to send.", reply_to=event.message
                )

            if is_range:
                final = (
                    f"✅ **SpideyBot:** Downloaded {result.get('downloaded_messages', len(files))} file(s) "
                    f"from {result.get('total_messages', len(files))} messages in `{result['chat_title']}`."
                )
            else:
                final = f"✅ **SpideyBot:** Downloaded {len(files)} file(s) from `{result['chat_title']}`."
            await status.close(final)
        except Exception as exc:
            logger.error("Failed to send TG files", error=str(exc))
            await status.close(f"❌ **SpideyBot:** Failed to send files: `{exc}`")
        finally:
            for fp in files:
                try:
                    os.remove(fp)
                except OSError:
                    pass
            try:
                os.rmdir(output_dir)
            except OSError:
                pass

    # ── /dl ──────────────────────────────────────────────────────

    @client.on(events.NewMessage(outgoing=True, pattern=re.compile(r"/dl", re.IGNORECASE)))
    async def dl_handler(event):
        match = re.match(r"/dl(?:\s+(https?://\S+))?", event.raw_text or "", re.IGNORECASE)
        if not match:
            return

        link = match.group(1)
        if not link:
            await _reply(client, event, "⚠️ Please specify a URL.\nUsage: `/dl <link>`")
            return

        dm = _download_manager
        if dm is None:
            await _reply(client, event, "❌ Bot is not ready yet.")
            return

        is_admin = user_id in config.ADMIN_IDS
        is_premium = has_premium_access(user_id)

        status_msg = await client.send_message(
            event.chat_id, "⏳ **SpideyBot:** Queueing your download request...", reply_to=event.message
        )
        try:
            status, task = await dm.add_task(user_id, event, link, is_premium, is_admin, status_msg)
            if status == "ok":
                pos = dm.get_queue_position(task.entry_id)
                await status_msg.edit(
                    f"⏳ **SpideyBot:** Task queued (position #{pos} in queue)\n"
                    f"Send `/cancel {task.entry_id}` to abort."
                )
        except Exception as exc:
            logger.error("Failed to queue /dl task", user_id=user_id, link=link, error=str(exc))
            try:
                await status_msg.edit(f"❌ **SpideyBot:** Failed to queue download: `{exc}`")
            except TelethonRPCError:
                pass

    # ── /cancel ──────────────────────────────────────────────────

    @client.on(events.NewMessage(outgoing=True, pattern=re.compile(r"/cancel(?:\s+(\d+))?", re.IGNORECASE)))
    async def cancel_handler(event):
        dm = _download_manager
        if dm is None:
            return

        match = re.match(r"/cancel(?:\s+(\d+))?", event.raw_text or "", re.IGNORECASE)
        target_id = match.group(1) if match else None

        if target_id:
            entry_id = int(target_id)
            task = dm.active_tasks.get(entry_id)
            if task and task.user_id == user_id and not task.is_cancelled:
                task.cancel()
                await _reply(client, event, f"❌ Task #{entry_id} cancelled.")
            else:
                await _reply(client, event, "❌ Task not found or already completed.")
            return

        user_tasks = dm.user_tasks(user_id)
        if not user_tasks:
            await _reply(client, event, "ℹ️ You have no active or queued tasks to cancel.")
            return

        if len(user_tasks) > 1:
            lines = ["**Your active tasks:**"]
            for task in user_tasks:
                pos = dm.get_queue_position(task.entry_id)
                status = f"queue #{pos}" if pos > 0 else "downloading"
                lines.append(f"  • `#{task.entry_id}` — {task.link[:50]}  ({status})")
            lines.append("\nUse `/cancel <id>` to cancel one, or send `/cancel` again to cancel all.")
            await _reply(client, event, "\n".join(lines))
            return

        task = user_tasks[0]
        task.cancel()
        await _reply(client, event, f"❌ Task #{task.entry_id} cancelled.")


async def _reply(client, event, text: str) -> None:
    try:
        await client.send_message(event.chat_id, text, reply_to=event.message)
    except TelethonRPCError:
        pass
