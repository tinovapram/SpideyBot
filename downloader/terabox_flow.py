"""
TeraBox download orchestration.

Pipelined: while uploading file N, file N+1 is downloading. Uploads are
grouped into albums of at most 10 and sent with flood-wait handling. All
phases report through one unified :class:`~utils.progress.StatusMessage`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time

import structlog

from core import config
from downloader.terabox import download_file_async
from utils import paths
from utils.files import build_caption, prepare_media
from utils.progress import StatusMessage, count_line
from utils.telethon import send_album

logger = structlog.get_logger(__name__)

_ALBUM_LIMIT = 10


async def run_terabox(task, client, downloader) -> None:
    """Execute a TeraBox download task end-to-end."""
    footer = f"Send `/cancel {task.entry_id}` to abort."
    if task.status_msg is not None:
        status = StatusMessage(task.status_msg, footer=footer)
    else:
        status = StatusMessage(
            await task.event.reply("⏳ **SpideyBot:** Starting download..."), footer=footer
        )

    if downloader is None:
        await status.close(
            "⚠️ **SpideyBot: TeraBox Downloader is not configured.**\n"
            "Please set `TERABOX_COOKIE` in the `.env` file."
        )
        return

    max_size_bytes, limit_str = config.get_size_limit(task.is_premium, task.is_admin)

    try:
        status.set_header("🔍 **SpideyBot:** Resolving TeraBox link...")

        saved_root = downloader.root_path
        downloader.root_path = f"/downloads/{task.user_id}/{int(time.time())}"
        try:
            result = await downloader.resolve(task.link, mode="download")
        finally:
            downloader.root_path = saved_root

        if not result.ok:
            await status.close(
                f"❌ **SpideyBot: Failed to resolve link.**\nReason: `{result.error or 'Unknown error'}`"
            )
            return

        files = sorted(
            (f for f in result.files if not f.is_dir and f.dlink),
            key=lambda f: f.size_bytes,
        )
        if not files:
            await status.close("ℹ️ **SpideyBot:** No actual files found in this share.")
            return

        total_bytes = sum(f.size_bytes for f in files)
        if total_bytes > max_size_bytes:
            await status.close(
                f"⚠️ **SpideyBot: Limit Exceeded.** Total share size is "
                f"`{total_bytes / (1024 * 1024):.2f} MB`, exceeding your limit of `{limit_str}`."
            )
            return

        output_dir = str(paths.DOWNLOADS_DIR / f"tb_{task.user_id}_{task.entry_id}")
        os.makedirs(output_dir, exist_ok=True)

        sent, failed = await _pipeline(task, client, downloader, files, output_dir, status)

        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)

        if task.is_cancelled:
            await status.close("❌ **SpideyBot:** Download cancelled.")
        elif sent > 0:
            text = (
                f"✅ **SpideyBot: Done!** Sent {sent}/{len(files)} files "
                f"({total_bytes / (1024 * 1024):.2f} MB)."
            )
            if failed:
                text += f"\n⚠️ {len(failed)} file(s) failed."
            await status.close(text)
        else:
            await status.close("❌ **SpideyBot:** No files were successfully sent.")
    except Exception as exc:
        logger.exception("Error processing TeraBox task", link=task.link, error=str(exc))
        await status.close(f"❌ **SpideyBot: An error occurred.**\nError: `{exc}`")


async def _pipeline(task, client, downloader, files, output_dir, status) -> tuple[int, list]:
    """Download files one-by-one while uploading finished ones, skipping failures.

    A failing file never stops the remaining files from being processed.
    Returns ``(sent_count, failed_files)``.
    """
    total = len(files)
    sent = 0
    failed: list[tuple[str, str]] = []
    downloaded = 0
    ready: asyncio.Queue = asyncio.Queue(maxsize=2)
    upload_cb = status.bytes_cb("ul", "📤", "Uploading")
    dl_cb = status.file_bytes_cb("cur", "📥")
    status.set_header("📥 **SpideyBot:** Downloading & uploading")

    def refresh() -> None:
        status.row("dl", count_line("📥", "Downloaded", downloaded, total))
        status.row("up", count_line("📤", "Sent", sent, total))
        if failed:
            status.row("fail", f"⚠️ Failed: {len(failed)} file(s)")
        else:
            status.drop("fail")

    async def producer() -> None:
        nonlocal downloaded
        for tb_file in files:
            if task.is_cancelled:
                break
            try:
                path = await download_file_async(
                    downloader, tb_file, output_dir, progress_callback=dl_cb
                )
                if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
                    failed.append((tb_file.filename, "empty file (0 bytes)"))
                else:
                    await ready.put((path, tb_file.filename))
                    downloaded += 1
            except Exception as exc:
                failed.append((tb_file.filename, str(exc) or type(exc).__name__))
            finally:
                status.drop("cur")
                refresh()
        await ready.put(None)

    pending_media: list = []
    pending_captions: list[str] = []

    async def flush() -> None:
        nonlocal sent, pending_media, pending_captions
        if not pending_media:
            return
        batch = pending_media
        captions = pending_captions
        pending_media = []
        pending_captions = []
        status.drop("ul")
        sent += await send_album(
            client, task.event.chat_id, batch, captions,
            reply_to=task.event.message.id, support_streaming=True,
        )
        refresh()

    async def consumer() -> None:
        while True:
            item = await ready.get()
            if item is None:
                break
            path, filename = item
            try:
                media = await prepare_media(client, path, progress_callback=upload_cb)
                pending_media.append(media)
                pending_captions.append(build_caption(filename, task.link))
                if len(pending_media) >= _ALBUM_LIMIT:
                    await flush()
            except Exception as exc:
                logger.warning("File send failed", file=path, error=str(exc))
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        await flush()

    await asyncio.gather(producer(), consumer())
    return sent, failed
