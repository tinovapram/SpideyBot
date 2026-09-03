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
from spideybot.utils.files import download_file_async

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


async def run_terabox(task, bot, terabox_downloader) -> None:
    """
    Execute a TeraBox download task end-to-end.

    Pipelined: while uploading batch N, downloading batch N+1 concurrently.

    Args:
        task: DownloadTask instance with user info and link.
        bot: Telethon TelegramClient instance.
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
        _batch_base = 0  # cumulative file count before current batch
        failed_files = []
        dl_done = 0          # files downloaded so far
        ul_done = 0          # files uploaded so far
        ul_pct = 0.0         # upload percentage from callback
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
            if ul_done > 0:
                parts.append(f"\U0001f4e4 Uploading #{ul_done}/{length_of_files} ({int(ul_pct)}%)")
            if not parts:
                return
            text = " \u2502 ".join(parts)
            try:
                await status_msg.edit(f"**SpideyBot:** {text}...")
            except (MessageNotModifiedError, TelethonRPCError):
                pass

        def _progress_cb(sent_f, total_f):
            """send_file progress callback: update upload count and per-file percentage."""
            nonlocal ul_done, ul_pct
            if isinstance(sent_f, float):
                # Album mode: float = file_index + fraction (e.g. 2.5 = 50% of 3rd file)
                # Add batch_base so the count is cumulative across batches (1-10, 11-20, …)
                ul_done = _batch_base + int(sent_f) + 1
                ul_pct = (sent_f % 1) * 100  # current file progress
            else:
                # Single file mode: sent_f = bytes, total_f = total bytes
                ul_done = _batch_base + 1
                ul_pct = (sent_f / total_f) * 100 if total_f else 0
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon(asyncio.ensure_future, _update_status())
            except RuntimeError:
                pass

        async def _send_batch(files):
            """Upload+send a batch via send_file."""
            nonlocal total_sent, _batch_base
            if not files:
                return
            _batch_base = total_sent
            valid = [f for f in files if os.path.exists(f) and os.path.getsize(f) > 0]
            if not valid:
                return
            try:
                await bot.send_file(
                    task.event.chat_id,
                    valid,
                    caption=caption,
                    reply_to=task.event.message.id,
                    supports_streaming=True,
                    progress_callback=_progress_cb,
                )
                total_sent += len(valid)
            except Exception as e:
                logger.warning("Batch send failed, falling back to individual", error=str(e))
                for fp in valid:
                    try:
                        await bot.send_file(
                            task.event.chat_id,
                            fp,
                            caption=caption,
                            reply_to=task.event.message.id,
                            supports_streaming=True,
                            progress_callback=_progress_cb,
                        )
                        total_sent += 1
                    except Exception as send_err:
                        logger.warning("Individual send failed", error=str(send_err))

        # --- Pipelined download + upload ---
        files_iter = iter(files_to_send)

        async def _prefetch_batch(count):
            """Download up to *count* files, updating dl_done."""
            nonlocal dl_done
            batch = []
            for _ in range(count):
                if task.is_cancelled:
                    break
                try:
                    tb_file = next(files_iter)
                except StopIteration:
                    break
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
                    continue

                if not filepath or not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                    logger.warning("Skipping 0-byte file", file=filepath)
                    failed_files.append((tb_file.filename, "empty file (0 bytes)"))
                    continue
                batch.append(filepath)
            return batch

        # Prefetch first batch, then pipeline: upload N || download N+1
        batch = await _prefetch_batch(_BATCH_SIZE)
        while batch:
            next_batch_task = asyncio.create_task(_prefetch_batch(_BATCH_SIZE))
            await _send_batch(batch)
            _cleanup_batch(batch)
            batch = await next_batch_task

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

