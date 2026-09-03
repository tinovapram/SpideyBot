"""
SpideyBot - TeraBox Download Handler.

Orchestrates the full TeraBox download flow: resolve -> download -> send -> cleanup.
Uses send_file (not upload_file) for single-call upload+send with progress.
Pipelined: while uploading batch N, downloading batch N+1 concurrently.
"""

import os
import time
import shutil
import asyncio

import structlog
from telethon.errors import RPCError as TelethonRPCError
from telethon.errors import MessageNotModifiedError

from spideybot.config import get_size_limit
from spideybot.utils.files import download_file_async, prepare_media_batch

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 10  # Telegram album limit


def _cleanup_batch(batch_files):
    """Remove downloaded files to free disk space."""
    for fp in batch_files:
        try:
            if os.path.isfile(fp):
                os.remove(fp)
        except OSError:
            pass


async def run_terabox(task, client, terabox_downloader) -> None:
    """
    Execute a TeraBox download task end-to-end.

    Pipelined: while uploading batch N, downloading batch N+1 concurrently.

    Args:
        task: DownloadTask instance with user info and link.
        client: Telethon TelegramClient instance.
        terabox_downloader: TeraBoxDownloader instance (or None if not configured).
    """
    status_msg = task.status_msg
    if not status_msg:
        status_msg = await task.event.reply("\u23f3 **SpideyBot:** Starting download...")

    if not terabox_downloader:
        await status_msg.edit(
            "\u26a0\ufe0f **SpideyBot: TeraBox Downloader is not configured.**\n"
            "Please ensure `TERABOX_COOKIE` is set in the `.env` file."
        )
        return

    try:
        await status_msg.edit("\U0001f50d **SpideyBot:** Resolving TeraBox link...")

        saved_root = terabox_downloader.root_path
        terabox_downloader.root_path = f"/downloads/{task.user_id}/terabox"
        try:
            result = await terabox_downloader.resolve(task.link, mode="download")
        finally:
            terabox_downloader.root_path = saved_root
        if not result.ok:
            await status_msg.edit(
                f"\u274c **SpideyBot: Failed to resolve link.**\n"
                f"Reason: `{result.error or 'Unknown error'}`"
            )
            return

        files_to_send = [f for f in result.files if not f.is_dir and f.dlink]
        if not files_to_send:
            await status_msg.edit("\u2139\ufe0f **SpideyBot:** No actual files found in this share.")
            return

        max_size_bytes, limit_str = get_size_limit(task.is_premium, task.is_admin)
        total_size_bytes = sum(f.size_bytes for f in files_to_send)
        total_size_mb = total_size_bytes / (1024 * 1024)
        if total_size_bytes > max_size_bytes:
            await status_msg.edit(
                f"\u26a0\ufe0f **SpideyBot: Limit Exceeded.** "
                f"The total share size is `{total_size_mb:.2f} MB`, "
                f"which exceeds your limit of `{limit_str}`.\nDownload aborted."
            )
            return

        files_to_send.sort(key=lambda f: f.size_bytes)
        length_of_files = len(files_to_send)
        title = result.title or "TeraBox Share"

        output_dir = f"./downloads/tb_{task.user_id}_{task.entry_id}"
        caption = f"{title}\n\nDownloaded by SpideyBot from [link]({task.link})\n\n"
        os.makedirs(output_dir, exist_ok=True)

        # --- Shared progress state ---
        total_sent = 0
        failed_files = []
        dl_done = 0          # files downloaded so far
        last_status_ts = 0.0 # throttle for status edits

        async def _update_status():
            """Build a combined download+upload status and edit (throttled)."""
            nonlocal last_status_ts
            now = time.time()
            if now - last_status_ts < 3.0:
                return
            last_status_ts = now
            parts = []
            if dl_done < length_of_files:
                dl_pct = int(dl_done / length_of_files * 100) if length_of_files else 0
                parts.append(f"\U0001f4e5 Downloading {dl_done}/{length_of_files} ({dl_pct}%)")
            if total_sent > 0:
                ul_pct = int(total_sent / length_of_files * 100) if length_of_files else 0
                parts.append(f"\U0001f4e4 Uploaded {total_sent}/{length_of_files} ({ul_pct}%)")
            if not parts:
                return
            text = " \u2502 ".join(parts)
            try:
                await status_msg.edit(f"**SpideyBot:** {text}...")
            except (MessageNotModifiedError, TelethonRPCError):
                pass

        def _progress_cb(sent, total):
            """upload_file progress callback — just triggers status refresh."""
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon(asyncio.ensure_future, _update_status())
            except RuntimeError:
                pass

        # --- Pipelined download + upload (one at a time) ---
        files_iter = iter(files_to_send)

        async def _prefetch_one():
            """Download next file, return path or None."""
            nonlocal dl_done
            if task.is_cancelled:
                return None
            try:
                tb_file = next(files_iter)
            except StopIteration:
                return None
            try:
                dl_done += 1
                await _update_status()
                filepath = await download_file_async(
                    terabox_downloader, tb_file, output_dir
                )
            except Exception as e:
                err = str(e) or f"{type(e).__name__}: {e}"
                logger.exception("Error downloading file", filename=tb_file.filename, error=err)
                failed_files.append((tb_file.filename, err))
                return None

            if not filepath or not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                logger.warning("Skipping 0-byte file", file=filepath)
                failed_files.append((tb_file.filename, "empty file (0 bytes)"))
                return None
            return filepath

        # Pipeline: prefetch N+1 while uploading N
        next_path = await _prefetch_one()
        while next_path:
            current_path = next_path
            next_task = asyncio.create_task(_prefetch_one())

            try:
                media = await prepare_media(
                    client, current_path, progress_callback=_progress_cb,
                )
                await client.send_file(
                    task.event.chat_id,
                    media,
                    caption=caption,
                    reply_to=task.event.message.id,
                )
                total_sent += 1
            except Exception as e:
                logger.warning("File send failed", file=current_path, error=str(e))

            # Cleanup uploaded file
            try:
                os.remove(current_path)
            except OSError:
                pass

            next_path = await next_task

        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)

        if task.is_cancelled:
            await status_msg.edit("\u274c **SpideyBot:** Download cancelled.")
        elif total_sent > 0:
            msg = (
                f"\u2705 **SpideyBot: Done!** Sent {total_sent}/{length_of_files} "
                f"files ({total_size_mb:.2f} MB)."
            )
            if failed_files:
                msg += f"\n\u26a0\ufe0f {len(failed_files)} file(s) failed."
            await status_msg.edit(msg)
        else:
            await status_msg.edit("\u274c **SpideyBot:** No files were successfully sent.")

    except Exception as e:
        logger.exception("Error processing TeraBox task", link=task.link, error=str(e))
        try:
            await status_msg.edit(
                f"\u274c **SpideyBot: An error occurred.**\nError: `{str(e)}`"
            )
        except (MessageNotModifiedError, TelethonRPCError):
            pass

