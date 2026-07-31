"""
SpideyBot - Media Download Handler.

Orchestrates the full download flow: download -> sanitize -> upload -> send -> cleanup.
Routes to Reddit, UniversalDownloader, or gallery-dl as fallback.
Extracted from queue_manager for single-responsibility.
"""

import os
import time
import shutil
import asyncio
from urllib.parse import urlparse

import structlog
from telethon import Button
from telethon.errors import RPCError as TelethonRPCError

from spideybot.config import get_size_limit
from spideybot.utils.files import sanitize_filename, extract_post_text
from spideybot.utils.progress import ProgressCallback
from spideybot.utils.task_progress import TaskProgress
from spideybot.downloaders.universal_downloader import UniversalDownloader

logger = structlog.get_logger(__name__)


async def run_download_task(task, bot, fallback_downloader, reddit_downloader=None) -> None:
    """
    Execute a media download task end-to-end.

    Flow:
        1. Download media via Reddit/UniversalDownloader, or gallery-dl as fallback
        2. Sanitize filenames for cross-platform compatibility
        3. Separate JSON metadata from media files
        4. Extract post caption from metadata
        5. Upload and send files to the user
        6. Cleanup temp directory

    Args:
        task: DownloadTask instance with user info and link.
        bot: Telethon TelegramClient instance.
        fallback_downloader: GalleryDLDownloader instance (used as generic fallback).
        reddit_downloader: RedditDownloader instance.
    """
    status_msg = task.status_msg
    if not status_msg:
        status_msg = await task.event.reply("⏳ **SpideyBot:** Starting download...")

    max_size_bytes, _ = get_size_limit(task.is_premium, task.is_admin)

    # Detect platform early for per-user staging dir
    _ud = UniversalDownloader()
    _platform = _ud.detect_platform(task.link)
    if "reddit.com" in task.link.lower():
        _site = "reddit"
    elif _platform != "unknown":
        _site = _platform
    else:
        _site = "gallery-dl"
    # Per-user staging dir: ./downloads/{user_id}/{site}/{entry_id}
    task_staging_dir = os.path.join(str(task.user_id), _site, str(task.entry_id))

    try:
        # Define throttled progress callback for Telegram status edits
        last_update_time = 0.0
        
        async def progress_callback(status_text: str):
            nonlocal last_update_time
            now = time.time()
            if now - last_update_time >= 5.0:  # Edit message at most once every 5 seconds to avoid rate limits
                try:
                    await status_msg.edit(status_text)
                    last_update_time = now
                except TelethonRPCError:
                    pass  # Telegram rate-limit or message deleted — safe to ignore

        async def run_twitter_fallback(dest_dir):
            from spideybot.downloaders.site_downloaders.twitter import TwitterDownloader
            td = TwitterDownloader()
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                td.download,
                task.link,
                dest_dir
            )

        # Download files
        is_twitter = any(domain in task.link.lower() for domain in ["twitter.com", "x.com"])
        is_reddit = "reddit.com" in task.link.lower()
        
        ud = _ud  # reuse pre-detected
        platform = _platform
        downloaded_files = None
        
        try:
            if is_reddit and reddit_downloader:
                logger.info("Attempting Reddit download via custom RedditDownloader")
                dest_dir = os.path.join(fallback_downloader.download_dir, task_staging_dir)
                loop = asyncio.get_running_loop()
                downloaded_files = await loop.run_in_executor(
                    None,
                    reddit_downloader.download,
                    task.link,
                    dest_dir
                )
            elif platform != "unknown":
                logger.info("Attempting download via UniversalDownloader", platform=platform)
                dest_dir = os.path.join(fallback_downloader.download_dir, task_staging_dir)
                loop = asyncio.get_running_loop()
                downloaded_files = await loop.run_in_executor(
                    None,
                    ud.download,
                    task.link,
                    dest_dir
                )
            else:
                downloaded_files = await fallback_downloader.download(
                    task.link, task_staging_dir, max_size_bytes, progress_callback=progress_callback
                )
        except Exception as e:
            if is_reddit and reddit_downloader:
                logger.warning("Custom RedditDownloader failed, falling back to gallery-dl", error=str(e))
                try:
                    downloaded_files = await fallback_downloader.download(
                        task.link, task_staging_dir, max_size_bytes, progress_callback=progress_callback
                    )
                except Exception as fallback_err:
                    raise fallback_err
            elif platform != "unknown":
                logger.warning("UniversalDownloader failed, falling back to gallery-dl", platform=platform, error=str(e))
                try:
                    downloaded_files = await fallback_downloader.download(
                        task.link, task_staging_dir, max_size_bytes, progress_callback=progress_callback
                    )
                except Exception as fallback_err:
                    if platform == "twitter":
                        logger.warning("gallery-dl failed for Twitter, trying fallback scraper", error=str(fallback_err))
                        dest_dir = os.path.join(fallback_downloader.download_dir, task_staging_dir)
                        downloaded_files = await run_twitter_fallback(dest_dir)
                    else:
                        raise fallback_err
            elif is_twitter:
                logger.warning("gallery-dl failed for Twitter, trying fallback scraper", error=str(e))
                dest_dir = os.path.join(fallback_downloader.download_dir, task_staging_dir)
                downloaded_files = await run_twitter_fallback(dest_dir)
            else:
                raise e

        if is_twitter and not downloaded_files:
            logger.info("gallery-dl returned no files for Twitter, trying fallback scraper")
            dest_dir = os.path.join(fallback_downloader.download_dir, task_staging_dir)
            downloaded_files = await run_twitter_fallback(dest_dir)

        if not downloaded_files:
            await status_msg.edit("📥 **SpideyBot: No files downloaded from the link.**")
            return

        # Sanitize paths to comply with Windows and Linux filename rules
        sanitized_paths = []
        for fp in downloaded_files:
            if os.path.exists(fp):
                directory, filename = os.path.split(fp)
                clean_name = sanitize_filename(filename)
                if clean_name != filename:
                    clean_fp = os.path.join(directory, clean_name)
                    try:
                        if os.path.exists(clean_fp) and clean_fp != fp:
                            base, ext = os.path.splitext(clean_name)
                            counter = 1
                            while os.path.exists(os.path.join(directory, f"{base}_{counter}{ext}")):
                                counter += 1
                            clean_fp = os.path.join(directory, f"{base}_{counter}{ext}")
                        os.rename(fp, clean_fp)
                        sanitized_paths.append(clean_fp)
                    except Exception as rename_err:
                        logger.warning("Failed to rename file", old=fp, new=clean_fp, error=str(rename_err))
                        sanitized_paths.append(fp)
                else:
                    sanitized_paths.append(fp)
            else:
                sanitized_paths.append(fp)
        downloaded_files = sanitized_paths

        # Separate metadata JSON files from media files
        json_files = [fp for fp in downloaded_files if fp.lower().endswith('.json')]
        media_files = [fp for fp in downloaded_files if not fp.lower().endswith('.json')]

        if not media_files:
            await status_msg.edit("ℹ️ **SpideyBot: No media files downloaded from the link.**")
            return

        # Extract post description/caption if present
        post_text = extract_post_text(json_files)

        # ── Unified Progress Tracker ─────────────────────────────────────
        parsed = urlparse(task.link)
        task_title = parsed.netloc or "Downloaded Media"
        progress = TaskProgress(task_title, total_files=len(media_files))
        for i, fp in enumerate(media_files):
            file_size = os.path.getsize(fp) if os.path.exists(fp) else 0
            progress.add_file(i, os.path.basename(fp), total_bytes=file_size)

        # Cancel button to persist through all progress messages
        _cancel_buttons = [[Button.inline("❌ Cancel", data=f"cancel:{task.entry_id}")]]

        await progress.update_message(status_msg, force=True, buttons=_cancel_buttons)

        # Upload files with per-file error tracking
        uploaded_handles = []
        failed_files = []
        for i, fp in enumerate(media_files):
            # Check cancellation before each upload
            if task.is_cancelled:
                progress.mark_skipped(i)
                await progress.update_message(status_msg, force=True, buttons=_cancel_buttons)
                continue

            try:
                progress.mark_uploading(i)
                await progress.update_message(status_msg, buttons=_cancel_buttons)

                def _make_cb(idx):
                    def cb(current, total):
                        if total:
                            progress.update_upload(idx, current)
                            progress.update_message(status_msg, buttons=_cancel_buttons)
                    return cb

                handle = await bot.upload_file(fp, progress_callback=_make_cb(i))
                uploaded_handles.append(handle)
                progress.mark_sent(i)
                await progress.update_message(status_msg, buttons=_cancel_buttons)
            except Exception as e:
                logger.error("Failed to upload file", file=fp, error=str(e))
                failed_files.append((os.path.basename(fp), str(e)))
                progress.mark_failed(i, error=str(e))
                await progress.update_message(status_msg, buttons=_cancel_buttons)
                await task.event.reply(f"❌ **Failed to upload file:** `{os.path.basename(fp)}`")

        # Finalize progress message
        await progress.finalize(status_msg)

        if uploaded_handles:
            await status_msg.edit("📤 **SpideyBot: Sending files to chat...**")

            # Get folder name from the first downloaded file as fallback caption
            first_file = media_files[0]
            folder_name = os.path.basename(os.path.dirname(first_file))
            if folder_name.startswith("dl_"):
                parsed = urlparse(task.link)
                folder_name = parsed.netloc or "Downloaded Media"

            final_caption = post_text if post_text else folder_name
            final_caption+='\n\nDownloaded by SpideyBot from [link]('+task.link+')\n\n'

            # try:
            #     await task.event.client.send_file(task.event.sender_id, file=uploaded_handles, caption=final_caption, progress_callback=task.progress_callback)
            
            # Attempt to send files as album if <= 10 files
            sent_success = False
            try:
                await task.event.reply(file=uploaded_handles, message=final_caption, supports_streaming=True)
                sent_success = True
            except Exception as album_err:
                logger.warning("Failed to send as album, falling back to individual", error=str(album_err))

            if not sent_success:
                # If sending individually, the first file gets the full post text,
                # subsequent ones get folder name/short caption
                for i, (fp, handle) in enumerate(zip(media_files, uploaded_handles)):
                    if i == 0 and post_text:
                        caption = post_text
                    else:
                        f_name = os.path.basename(os.path.dirname(fp))
                        if f_name.startswith("dl_"):
                            parsed = urlparse(task.link)
                            f_name = parsed.netloc or "Downloaded Media"
                        caption = f_name
                    try:
                        await task.event.reply(file=handle, message=caption)
                    except TelethonRPCError:
                        pass

            await status_msg.edit(f"✅ **SpideyBot: Download completed!**\n• Sent {len(uploaded_handles)} file(s).")
        else:
            await status_msg.edit("❌ **SpideyBot: No files were successfully downloaded.**")

        # Report per-file failures if any
        error_summary = progress.get_error_summary()
        if error_summary:
            await task.event.reply(error_summary)

    except Exception as e:
        logger.exception("Error in gallery-dl download task", link=task.link, error=str(e))
        await status_msg.edit(f"❌ **SpideyBot: Failed to download media.**\nReason: `{str(e)}`")

    finally:
        # Cleanup downloaded files
        dest_dir = os.path.join(fallback_downloader.download_dir, task_staging_dir)
        if os.path.exists(dest_dir):
            shutil.rmtree(dest_dir)
