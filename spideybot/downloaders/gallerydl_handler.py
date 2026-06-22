"""
SpideyBot — Gallery-dl Download Handler.

Orchestrates the full gallery-dl download flow: download → sanitize → upload → send → cleanup.
Extracted from queue_manager._run_gallerydl for single-responsibility.
"""

import os
import shutil
import logging
import asyncio
from urllib.parse import urlparse

from spideybot.config import get_size_limit
from spideybot.utils.files import sanitize_filename, extract_post_text
from spideybot.utils.progress import ProgressCallback
from spideybot.downloaders.universal_downloader import UniversalDownloader

logger = logging.getLogger(__name__)


async def run_gallerydl(task, bot, gallerydl_downloader, reddit_downloader=None):
    """
    Execute a gallery-dl download task end-to-end.

    Flow:
        1. Download media via gallery-dl or custom RedditDownloader/UniversalDownloader (with fallback)
        2. Sanitize filenames for cross-platform compatibility
        3. Separate JSON metadata from media files
        4. Extract post caption from metadata
        5. Upload and send files to the user
        6. Cleanup temp directory

    Args:
        task: DownloadTask instance with user info and link.
        bot: Telethon TelegramClient instance.
        gallerydl_downloader: GalleryDLDownloader instance.
        reddit_downloader: RedditDownloader instance.
    """
    status_msg = task.status_msg
    if not status_msg:
        status_msg = await task.event.reply("⏳ **SpideyBot:** Starting download...")

    max_size_bytes, _ = get_size_limit(task.is_premium, task.is_admin)
    task_dir_id = f"gdl_{task.user_id}_{task.entry_id}"

    try:
        # Define throttled progress callback for Telegram status edits
        import time
        last_update_time = 0.0
        
        async def progress_callback(status_text: str):
            nonlocal last_update_time
            now = time.time()
            if now - last_update_time >= 5.0:  # Edit message at most once every 4 seconds to avoid rate limits
                try:
                    await status_msg.edit(status_text)
                    last_update_time = now
                except Exception:
                    pass

        async def run_twitter_fallback(dest_dir):
            from spideybot.downloaders.site_downloaders.twitter import TwitterDownloader
            td = TwitterDownloader()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                td.download,
                task.link,
                dest_dir
            )

        # Download files
        is_twitter = any(domain in task.link.lower() for domain in ["twitter.com", "x.com"])
        is_reddit = "reddit.com" in task.link.lower()
        
        ud = UniversalDownloader()
        platform = ud.detect_platform(task.link)
        downloaded_files = None
        
        try:
            if is_reddit and reddit_downloader:
                logger.info("Attempting Reddit download via custom RedditDownloader...")
                dest_dir = os.path.join(gallerydl_downloader.download_dir, task_dir_id)
                loop = asyncio.get_event_loop()
                downloaded_files = await loop.run_in_executor(
                    None,
                    reddit_downloader.download,
                    task.link,
                    dest_dir
                )
            elif platform != "unknown":
                logger.info(f"Attempting download via custom UniversalDownloader ({platform})...")
                dest_dir = os.path.join(gallerydl_downloader.download_dir, task_dir_id)
                loop = asyncio.get_event_loop()
                downloaded_files = await loop.run_in_executor(
                    None,
                    ud.download,
                    task.link,
                    dest_dir
                )
            else:
                downloaded_files = await gallerydl_downloader.download(
                    task.link, task_dir_id, max_size_bytes, progress_callback=progress_callback
                )
        except Exception as e:
            if is_reddit and reddit_downloader:
                logger.warning(f"Custom RedditDownloader failed: {e}. Falling back to gallery-dl...")
                try:
                    downloaded_files = await gallerydl_downloader.download(
                        task.link, task_dir_id, max_size_bytes, progress_callback=progress_callback
                    )
                except Exception as fallback_err:
                    raise fallback_err
            elif platform != "unknown":
                logger.warning(f"UniversalDownloader failed for {platform}: {e}. Falling back to gallery-dl...")
                try:
                    downloaded_files = await gallerydl_downloader.download(
                        task.link, task_dir_id, max_size_bytes, progress_callback=progress_callback
                    )
                except Exception as fallback_err:
                    if platform == "twitter":
                        logger.warning(f"gallery-dl failed for Twitter link: {fallback_err}. Trying fallback scraper...")
                        dest_dir = os.path.join(gallerydl_downloader.download_dir, task_dir_id)
                        downloaded_files = await run_twitter_fallback(dest_dir)
                    else:
                        raise fallback_err
            elif is_twitter:
                logger.warning(f"gallery-dl failed for Twitter link: {e}. Trying fallback scraper...")
                dest_dir = os.path.join(gallerydl_downloader.download_dir, task_dir_id)
                downloaded_files = await run_twitter_fallback(dest_dir)
            else:
                raise e

        if is_twitter and not downloaded_files:
            logger.info("gallery-dl returned no files for Twitter link. Trying fallback scraper...")
            dest_dir = os.path.join(gallerydl_downloader.download_dir, task_dir_id)
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
                        logger.warning(f"Failed to rename {fp} to {clean_fp}: {rename_err}")
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
            await status_msg.edit("ℹ️ **SpideyBot: No media files downloaded from the link.**")
            return

        await status_msg.edit(f"📤 **SpideyBot: Uploading {len(media_files)} downloaded file(s)...**")

        # Extract post description/caption if present
        post_text = extract_post_text(json_files)

        # Upload files
        uploaded_handles = []
        for i, fp in enumerate(media_files):
            try:
                callback = ProgressCallback(
                    task.event,
                    status_msg,
                    prefix_text=f"📤 **SpideyBot: Uploading media...**",
                    file_index=i+1,
                    total_files=len(media_files)
                )
                handle = await bot.upload_file(fp, progress_callback=callback.update)
                uploaded_handles.append(handle)
            except Exception as e:
                logger.error(f"Failed to upload file {fp}: {e}")
                await task.event.reply(f"❌ **Failed to upload file:** `{os.path.basename(fp)}`")

        if uploaded_handles:
            await status_msg.edit("📤 **SpideyBot: Sending files to chat...**")

            # Get folder name from the first downloaded file as fallback caption
            first_file = media_files[0]
            folder_name = os.path.basename(os.path.dirname(first_file))
            if folder_name.startswith("gdl_"):
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
                logger.warning(f"Failed to send as album, falling back to individual: {album_err}")

            if not sent_success:
                # If sending individually, the first file gets the full post text,
                # subsequent ones get folder name/short caption
                for i, (fp, handle) in enumerate(zip(media_files, uploaded_handles)):
                    if i == 0 and post_text:
                        caption = post_text
                    else:
                        f_name = os.path.basename(os.path.dirname(fp))
                        if f_name.startswith("gdl_"):
                            parsed = urlparse(task.link)
                            f_name = parsed.netloc or "Downloaded Media"
                        caption = f_name
                    try:
                        await task.event.reply(file=handle, message=caption)
                    except Exception:
                        pass

            await status_msg.edit(f"✅ **SpideyBot: Download completed!**\n• Sent {len(uploaded_handles)} file(s).")
        else:
            await status_msg.edit("❌ **SpideyBot: No files were successfully downloaded.**")

    except Exception as e:
        logger.exception(f"Error in gallery-dl download task: {e}")
        await status_msg.edit(f"❌ **SpideyBot: Failed to download media.**\nReason: `{str(e)}`")

    finally:
        # Cleanup downloaded files
        dest_dir = os.path.join(gallerydl_downloader.download_dir, task_dir_id)
        if os.path.exists(dest_dir):
            shutil.rmtree(dest_dir)
