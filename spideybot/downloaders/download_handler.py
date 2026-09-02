"""
SpideyBot - Media Download Handler.

Orchestrates the full download flow: download -> sanitize -> upload -> send -> cleanup.
Routes to Reddit, UniversalDownloader, or gallery-dl as fallback.
Extracted from queue_manager for single-responsibility.

Supports streaming (incremental) downloads for site-specific downloaders:
files are yielded one-by-one, batched into groups of 10, and sent as an
album while disk space is reclaimed immediately after each batch.
Gallery-dl downloads everything at once (no streaming) and sends all at once.
"""

import os
import time
import shutil
import asyncio
from urllib.parse import urlparse

import structlog
from telethon.errors import RPCError as TelethonRPCError

from spideybot.config import get_size_limit
from spideybot.utils.files import sanitize_filename, extract_post_text
from spideybot.downloaders.universal_downloader import UniversalDownloader  # noqa: F401

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 10  # Telegram album limit


def _sanitize_path(fp):
    """Rename *fp* on disk if the filename needs sanitization."""
    if not os.path.exists(fp):
        return fp
    directory, filename = os.path.split(fp)
    clean_name = sanitize_filename(filename)
    if clean_name == filename:
        return fp
    clean_fp = os.path.join(directory, clean_name)
    try:
        if os.path.exists(clean_fp) and clean_fp != fp:
            base, ext = os.path.splitext(clean_name)
            counter = 1
            while os.path.exists(os.path.join(directory, f"{base}_{counter}{ext}")):
                counter += 1
            clean_fp = os.path.join(directory, f"{base}_{counter}{ext}")
        os.rename(fp, clean_fp)
        return clean_fp
    except Exception as e:
        logger.warning("Failed to rename file", old=fp, new=clean_fp, error=str(e))
        return fp


def _cleanup_batch(batch_files):
    """Remove downloaded files to free disk space."""
    for fp in batch_files:
        try:
            if os.path.isfile(fp):
                os.remove(fp)
        except OSError:
            pass


async def _upload_and_send_batch(bot, task, batch_files, caption, status_msg):
    """Send *batch_files* as album via send_file. Returns count sent."""
    sent = 0
    last_update = 0.0
    n = len(batch_files)

    def _progress_cb(sent_f, total_f):
        nonlocal last_update
        now = time.time()
        if now - last_update < 3.0:
            return
        last_update = now
        # For albums: sent_f is float (e.g. 2.5 = 50% of 3rd file)
        file_idx = int(sent_f) + 1
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(
                asyncio.ensure_future,
                status_msg.edit(
                    f"\U0001f4e4 **SpideyBot:** Sending {file_idx}/{n}..."
                ),
            )
        except RuntimeError:
            pass

    # Filter out 0-byte files
    valid = [fp for fp in batch_files if os.path.exists(fp) and os.path.getsize(fp) > 0]
    if not valid:
        return 0

    try:
        await bot.send_file(
            task.event.chat_id,
            valid,
            caption=caption,
            reply_to=task.event.message.id,
            supports_streaming=True,
            progress_callback=_progress_cb,
        )
        sent = len(valid)
    except Exception as e:
        logger.warning("Album send failed, falling back to individual", error=str(e))
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
                sent += 1
            except Exception as send_err:
                logger.warning("Individual send failed", error=str(send_err))
    return sent


async def _send_streaming(
    task, bot, dest_dir, source_iter, fallback_downloader,
    status_msg, caption, max_size_bytes, progress_callback,
):
    """Consume *source_iter* in a pipelined fashion: download next batch
    while uploading the current batch. Returns total sent."""
    total_sent = 0
    json_files = []
    loop = asyncio.get_running_loop()

    def _prefetch_batch():
        """Run in executor: consume up to _BATCH_SIZE items from source_iter."""
        batch = []
        for _ in range(_BATCH_SIZE):
            try:
                file_path = next(source_iter)
            except StopIteration:
                break
            clean = _sanitize_path(file_path)
            if clean.lower().endswith(".json"):
                json_files.append(clean)
                continue
            batch.append(clean)
        return batch

    try:
        # Prefetch first batch (blocking in executor thread)
        batch = await loop.run_in_executor(None, _prefetch_batch)

        while batch:
            # Start prefetching next batch in background thread
            next_batch_task = loop.run_in_executor(None, _prefetch_batch)
            # Send current batch (async, overlaps with prefetch)
            total_sent += await _upload_and_send_batch(
                bot, task, batch, caption, status_msg
            )
            _cleanup_batch(batch)
            # Wait for next batch to be ready
            batch = await next_batch_task
    except Exception as e:
        logger.warning(
            "Streaming download failed, falling back to gallery-dl", error=str(e)
        )
        try:
            await status_msg.edit(
                "\u26a0\ufe0f **SpideyBot:** Primary download failed, "
                "trying gallery-dl fallback..."
            )
        except TelethonRPCError:
            pass
        downloaded = await fallback_downloader.download(
            task.link,
            os.path.join(dest_dir, "gdl_fallback"),
            max_size_bytes,
            progress_callback=progress_callback,
        )
        if downloaded:
            total_sent = await _send_all_at_once(
                bot, task, downloaded, caption, status_msg
            )

    # Cleanup metadata + staging dir
    for fp in json_files:
        try:
            if os.path.isfile(fp):
                os.remove(fp)
        except OSError:
            pass
    try:
        if os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir, ignore_errors=True)
    except Exception:
        pass
    return total_sent


async def _send_all_at_once(bot, task, downloaded_files, caption, status_msg):
    """Send all files at once via send_file (gallery-dl path). Returns count sent."""
    media_files = []
    for fp in downloaded_files:
        if os.path.exists(fp) and not fp.lower().endswith(".json"):
            media_files.append(_sanitize_path(fp))

    if not media_files:
        return 0

    n = len(media_files)
    last_update = 0.0
    sent = 0

    def _progress_cb(sent_f, total_f):
        nonlocal last_update
        now = time.time()
        if now - last_update < 3.0:
            return
        last_update = now
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(
                asyncio.ensure_future,
                status_msg.edit(
                    f"\U0001f4e4 **SpideyBot:** Sending {int(sent_f) + 1}/{n}..."
                ),
            )
        except RuntimeError:
            pass

    valid = [fp for fp in media_files if os.path.exists(fp) and os.path.getsize(fp) > 0]
    if not valid:
        return 0

    try:
        await bot.send_file(
            task.event.chat_id,
            valid,
            caption=caption,
            reply_to=task.event.message.id,
            supports_streaming=True,
            progress_callback=_progress_cb,
        )
        sent = len(valid)
    except Exception as e:
        logger.warning("Album send failed, falling back to individual", error=str(e))
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
                sent += 1
            except Exception as send_err:
                logger.warning("Individual send failed", error=str(send_err))
    return sent


async def run_download_task(task, bot, fallback_downloader, reddit_downloader=None) -> None:
    """
    Execute a media download task end-to-end.

    Streaming path (site-specific downloaders): files yielded one-by-one,
    batched into groups of 10, sent as albums, cleaned up immediately.
    Gallery-dl path: downloads everything, sends all at once.

    Args:
        task: DownloadTask instance with user info and link.
        bot: Telethon TelegramClient instance.
        fallback_downloader: GalleryDLDownloader instance.
        reddit_downloader: RedditDownloader instance.
    """
    status_msg = task.status_msg
    if not status_msg:
        status_msg = await task.event.reply("\u23f3 **SpideyBot:** Starting download...")

    max_size_bytes, _ = get_size_limit(task.is_premium, task.is_admin)

    _ud = UniversalDownloader()
    _platform = _ud.detect_platform(task.link)
    if "reddit.com" in task.link.lower():
        _site = "reddit"
    elif _platform != "unknown":
        _site = _platform
    else:
        _site = "gallery-dl"
    task_staging_dir = os.path.join(str(task.user_id), _site, str(task.entry_id))
    dest_dir = os.path.join(fallback_downloader.download_dir, task_staging_dir)

    try:
        last_update_time = 0.0

        async def progress_callback(status_text: str):
            nonlocal last_update_time
            now = time.time()
            if now - last_update_time >= 5.0:
                try:
                    await status_msg.edit(status_text)
                    last_update_time = now
                except TelethonRPCError:
                    pass

        async def _edit_status(text: str):
            try:
                await status_msg.edit(text)
            except TelethonRPCError:
                pass

        is_reddit = "reddit.com" in task.link.lower()
        site_label = "Reddit" if is_reddit else (
            _platform if _platform != "unknown" else "gallery-dl"
        )
        use_streaming = _platform != "unknown"

        try:
            await _edit_status(
                f"\U0001f4e5 **SpideyBot:** Downloading from {site_label}..."
            )

            if use_streaming:
                # Streaming path: yield files one-by-one, batch-send
                loop = asyncio.get_running_loop()
                if is_reddit and reddit_downloader:
                    logger.info("Streaming download via RedditDownloader")
                    source_iter = await loop.run_in_executor(
                        None,
                        lambda: reddit_downloader.download_streaming(
                            task.link, dest_dir
                        ),
                    )
                else:
                    logger.info(
                        "Streaming download via UniversalDownloader",
                        platform=_platform,
                    )
                    source_iter = await loop.run_in_executor(
                        None,
                        lambda: _ud.download_streaming(task.link, dest_dir),
                    )

                parsed = urlparse(task.link)
                folder_name = parsed.netloc or "Downloaded Media"
                caption = (
                    folder_name
                    + f"\n\nDownloaded by SpideyBot from [link]({task.link})\n\n"
                )

                total_sent = await _send_streaming(
                    task, bot, dest_dir, source_iter, fallback_downloader,
                    status_msg, caption, max_size_bytes, progress_callback,
                )
            else:
                # Gallery-dl path: downloads all at once
                logger.info("Downloading via gallery-dl")
                downloaded_files = await fallback_downloader.download(
                    task.link, task_staging_dir, max_size_bytes,
                    progress_callback=progress_callback,
                )

                if not downloaded_files:
                    await status_msg.edit(
                        "\u274c **SpideyBot: No files downloaded from the link.**"
                    )
                    return

                json_files = [
                    f for f in downloaded_files if f.lower().endswith(".json")
                ]
                post_text = extract_post_text(json_files)
                parsed = urlparse(task.link)
                folder_name = parsed.netloc or "Downloaded Media"
                caption = (post_text or folder_name) + (
                    f"\n\nDownloaded by SpideyBot from [link]({task.link})\n\n"
                )

                total_sent = await _send_all_at_once(
                    bot, task, downloaded_files, caption, status_msg
                )

                staging = os.path.join(
                    fallback_downloader.download_dir, task_staging_dir
                )
                if os.path.isdir(staging):
                    shutil.rmtree(staging, ignore_errors=True)

        except Exception as e:
            logger.exception("Download failed", link=task.link, error=str(e))
            await status_msg.edit(
                f"\u274c **SpideyBot: Failed to download media.**\n"
                f"Reason: `{str(e)}`"
            )
            return

        if total_sent > 0:
            await status_msg.edit(
                f"\u2705 **SpideyBot: Done!** Sent {total_sent} file(s)."
            )
        else:
            await status_msg.edit(
                "\u274c **SpideyBot: No files were successfully sent.**"
            )

    except Exception as e:
        logger.exception(
            "Unhandled error in download task", link=task.link, error=str(e)
        )
        try:
            await status_msg.edit(
                f"\u274c **SpideyBot: Failed to download media.**\n"
                f"Reason: `{str(e)}`"
            )
        except TelethonRPCError:
            pass
