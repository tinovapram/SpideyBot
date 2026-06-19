"""
SpideyBot — TeraBox Download Handler.

Orchestrates the full TeraBox download flow: resolve → download → upload → send → cleanup.
Extracted from queue_manager._run_terabox for single-responsibility.
"""

import os
import shutil
import logging

from spideybot.config import get_size_limit
from spideybot.utils.files import download_file_async
from spideybot.utils.progress import ProgressCallback

logger = logging.getLogger(__name__)


async def run_terabox(task, bot, tb_downloader):
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
        tb_downloader: TeraBoxDownloader instance (or None if not configured).
    """
    status_msg = task.status_msg
    if not status_msg:
        status_msg = await task.event.reply("⏳ **SpideyBot:** Starting download...")

    if not tb_downloader:
        await status_msg.edit(
            "⚠️ **SpideyBot: TeraBox Downloader is not configured.**\n"
            "Please ensure `TERABOX_COOKIE` is set in the `.env` file."
        )
        return

    try:
        await status_msg.edit("🔍 **SpideyBot: Resolving TeraBox link...**")

        result = tb_downloader.resolve(task.link, mode="download")
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

        for i, tb_file in enumerate(files_to_send):
            await status_msg.edit(
                f"📥 **SpideyBot: Downloading share...**\n"
                f"• **Title:** `{title}`\n"
                f"• **Files:** {length_of_files} ({total_size_mb:.2f} MB)\n"
                f"• **Status:** Downloading file {i+1}/{length_of_files} (`{tb_file.filename}` - {tb_file.size_mb:.2f} MB)"
            )

            try:
                filepath = await download_file_async(tb_downloader, tb_file, output_dir)

                callback = ProgressCallback(
                    task.event,
                    status_msg,
                    prefix_text=f"📤 **SpideyBot: Uploading share...**\n• **Title:** `{title}`\n• **Files:** {length_of_files} ({total_size_mb:.2f} MB)",
                    file_index=i+1,
                    total_files=length_of_files
                )
                file_handle = await bot.upload_file(filepath, progress_callback=callback.update)
                if file_handle:
                    files_handlers.append(file_handle)

                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception as file_err:
                logger.exception(f"Error processing file {tb_file.filename}: {file_err}")
                await task.event.reply(f"❌ **Failed to send file:** `{tb_file.filename}`\nError: `{str(file_err)}`")

        if files_handlers:
            await status_msg.edit(
                f"📤 **SpideyBot: Finalizing upload...**\n"
                f"• **Title:** `{title}`\n"
                f"• **Status:** Sending files to chat..."
            )
            try:
                await task.event.reply(message=f"{title}\n\nDownloaded by SpideyBot from [link]({task.link})\n\n", file=files_handlers, supports_streaming=True)
                await status_msg.edit(
                    f"✅ **SpideyBot: Download completed!**\n"
                    f"• **Title:** `{title}`\n"
                    f"• **Files:** {length_of_files} ({total_size_mb:.2f} MB) successfully sent."
                )
            except Exception as e:
                logger.exception(f"Error sending files: {e}")
                await status_msg.edit(f"❌ **SpideyBot: Error sending files.**\nError: `{str(e)}`")
        else:
            await status_msg.edit("❌ **SpideyBot: No files were successfully downloaded.**")

        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)

    except Exception as e:
        logger.exception(f"Error processing TeraBox task: {e}")
        await status_msg.edit(f"❌ **SpideyBot: An error occurred.**\nError: `{str(e)}`")
