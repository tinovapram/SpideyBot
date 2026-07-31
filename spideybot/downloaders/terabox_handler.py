"""
SpideyBot — TeraBox Download Handler.

Orchestrates the full TeraBox download flow: resolve → download → upload → send → cleanup.
Extracted from queue_manager._run_terabox for single-responsibility.
"""

import os
import shutil
import asyncio
from dataclasses import dataclass
from typing import Optional, List

import structlog
from telethon import Button

from spideybot.config import get_size_limit
from spideybot.utils.files import download_file_async
from spideybot.utils.progress import ProgressCallback
from spideybot.utils.task_progress import TaskProgress

logger = structlog.get_logger(__name__)

# Pipeline buffer: how many files to download ahead of upload
_PIPELINE_BUFFER = 2


@dataclass
class _PipelineItem:
    """An item flowing through the download→upload pipeline."""
    index: int
    filename: str
    filepath: Optional[str] = None
    error: Optional[str] = None


async def run_terabox(task, bot, terabox_downloader) -> None:
    """
    Execute a TeraBox download task end-to-end.

    Flow:
        1. Resolve the TeraBox share link to get file metadata
        2. Enforce per-tier size limits
        3. Download each file to a temp directory
        4. Upload each file to Telegram
        5. Send files to the user as an album
        6. Cleanup temp files

    Args:
        task: DownloadTask instance with user info and link.
        bot: Telethon TelegramClient instance.
        terabox_downloader: TeraBoxDownloader instance (or None if not configured).
    """
    status_msg = task.status_msg
    if not status_msg:
        status_msg = await task.event.reply("⏳ **SpideyBot:** Starting download...")

    if not terabox_downloader:
        await status_msg.edit(
            "⚠️ **SpideyBot: TeraBox Downloader is not configured.**\n"
            "Please ensure `TERABOX_COOKIE` is set in the `.env` file."
        )
        return

    try:
        await status_msg.edit("🔍 **SpideyBot: Resolving TeraBox link...**")

        # Per-user staging folder: /downloads/{user_id}/terabox
        saved_root = terabox_downloader.root_path
        terabox_downloader.root_path = f"/downloads/{task.user_id}/terabox"
        try:
            result = await terabox_downloader.resolve(task.link, mode="download")
        finally:
            terabox_downloader.root_path = saved_root
        if not result.ok:
            await status_msg.edit(f"❌ **SpideyBot: Failed to resolve link.**\nReason: `{result.error or 'Unknown error'}`")
            return

        files_to_send = [f for f in result.files if not f.is_dir and f.dlink]
        if not files_to_send:
            await status_msg.edit("ℹ️ **SpideyBot: No actual files found in this share.**")
            return

        # Size limits
        max_size_bytes, limit_str = get_size_limit(task.is_premium, task.is_admin)

        total_size_bytes = sum(f.size_bytes for f in files_to_send)
        total_size_mb = total_size_bytes / (1024 * 1024)
        if total_size_bytes > max_size_bytes:
            await status_msg.edit(
                f"⚠️ **SpideyBot: Limit Exceeded.** The total share size is `{total_size_mb:.2f} MB`, "
                f"which exceeds your limit of `{limit_str}`.\n"
                f"Download aborted."
            )
            return

        files_to_send.sort(key=lambda f: f.size_bytes)
        length_of_files = len(files_to_send)
        title = result.title or "TeraBox Share"

        output_dir = f"./downloads/tb_{task.user_id}_{task.entry_id}"
        files_handlers = []
        failed_files = []

        # ── Unified Progress Tracker ─────────────────────────────────────
        progress = TaskProgress(title, total_files=length_of_files)
        for i, tb_file in enumerate(files_to_send):
            progress.add_file(i, tb_file.filename, total_bytes=tb_file.size_bytes)

        # Cancel button to persist through all progress messages
        _cancel_buttons = [[Button.inline("❌ Cancel", data=f"cancel:{task.entry_id}")]]

        # ── Pipeline: download next while uploading current ──────────────
        dl_queue: asyncio.Queue[_PipelineItem] = asyncio.Queue(maxsize=_PIPELINE_BUFFER)

        async def _producer():
            """Download files and push them into the pipeline queue."""
            for i, tb_file in enumerate(files_to_send):
                if task.is_cancelled:
                    break
                item = _PipelineItem(index=i, filename=tb_file.filename)
                try:
                    progress.mark_downloading(i, total_bytes=tb_file.size_bytes)
                    await progress.update_message(status_msg, buttons=_cancel_buttons)
                    item.filepath = await download_file_async(terabox_downloader, tb_file, output_dir)
                except Exception as file_err:
                    # str(TimeoutError()) can be empty — always produce a
                    # truthy error string so the consumer never falls through.
                    err_msg = str(file_err) or f"{type(file_err).__name__}: {file_err}"
                    logger.exception("Error downloading file", filename=tb_file.filename, error=err_msg)
                    item.error = err_msg
                await dl_queue.put(item)

            # Sentinel: signal producer is done
            await dl_queue.put(_PipelineItem(index=-1, filename="__DONE__"))

        async def _consumer():
            """Upload files as they arrive from the download queue."""
            nonlocal files_handlers
            while True:
                item = await dl_queue.get()
                if item.index == -1:
                    break  # Producer is done

                # Check cancellation before upload
                if task.is_cancelled:
                    if item.filepath and os.path.exists(item.filepath):
                        os.remove(item.filepath)
                    progress.mark_skipped(item.index)
                    await progress.update_message(status_msg, buttons=_cancel_buttons)
                    continue

                # Defensive: catch both truthy error AND missing filepath
                if item.error or item.filepath is None:
                    err = item.error or "Download produced no file"
                    failed_files.append((item.filename, err))
                    progress.mark_failed(item.index, error=err)
                    await progress.update_message(status_msg, buttons=_cancel_buttons)
                    await task.event.reply(f"❌ **Failed to download file:** `{item.filename}`\nError: `{err}`")
                    continue

                try:
                    progress.mark_uploading(item.index)
                    await progress.update_message(status_msg, buttons=_cancel_buttons)

                    # Create a progress callback that feeds into TaskProgress
                    def _make_cb(idx):
                        def cb(current, total):
                            if total:
                                progress.update_upload(idx, current)
                                progress.update_message(status_msg, buttons=_cancel_buttons)
                        return cb

                    file_handle = await bot.upload_file(
                        item.filepath,
                        progress_callback=_make_cb(item.index)
                    )
                    if file_handle:
                        files_handlers.append(file_handle)
                    progress.mark_sent(item.index)
                    await progress.update_message(status_msg, buttons=_cancel_buttons)

                    # Clean up downloaded file immediately after upload
                    if item.filepath and os.path.exists(item.filepath):
                        os.remove(item.filepath)
                except Exception as upload_err:
                    logger.exception("Error uploading file", filename=item.filename, error=str(upload_err))
                    failed_files.append((item.filename, str(upload_err)))
                    progress.mark_failed(item.index, error=str(upload_err))
                    await progress.update_message(status_msg, buttons=_cancel_buttons)
                    await task.event.reply(f"❌ **Failed to upload file:** `{item.filename}`\nError: `{str(upload_err)}`")
                    if item.filepath and os.path.exists(item.filepath):
                        os.remove(item.filepath)

        # Run producer and consumer concurrently
        await asyncio.gather(_producer(), _consumer())

        # Finalize progress message
        await progress.finalize(status_msg)

        if files_handlers:
            if task.is_cancelled:
                await status_msg.edit("❌ **SpideyBot:** Download cancelled.")
            else:
                await status_msg.edit(
                    f"📤 **SpideyBot: Finalizing upload...**\n"
                    f"• **Title:** `{title}`\n"
                    f"• **Status:** Sending files to chat..."
                )
                try:
                    await task.event.reply(message=f"{title}\n\nDownloaded by SpideyBot from [link]({task.link})\n\n", file=files_handlers, supports_streaming=True)
                    success_count = len(files_handlers)
                    msg = f"✅ **SpideyBot: Download completed!**\n• **Title:** `{title}`\n• **Files:** {success_count}/{length_of_files} ({total_size_mb:.2f} MB) successfully sent."
                    if failed_files:
                        msg += f"\n⚠️ {len(failed_files)} file(s) failed."
                    await status_msg.edit(msg)
                except Exception as e:
                    logger.exception("Error sending files to chat", error=str(e))
                    await status_msg.edit(f"❌ **SpideyBot: Error sending files.**\nError: `{str(e)}`")
        else:
            await status_msg.edit("❌ **SpideyBot: No files were successfully downloaded.**")

        # Report per-file failures if any
        error_summary = progress.get_error_summary()
        if error_summary:
            await task.event.reply(error_summary)

        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)

    except Exception as e:
        logger.exception("Error processing TeraBox task", link=task.link, error=str(e))
        await status_msg.edit(f"❌ **SpideyBot: An error occurred.**\nError: `{str(e)}`")
