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
from telethon.errors import MessageNotModifiedError

from spideybot.config import get_size_limit
from spideybot.utils.files import sanitize_filename, extract_post_text, prepare_media
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


def _build_file_caption(filename, link):
    """Build per-file caption with filename and download footer."""
    return f"{filename}\n\nDownloaded by SpideyBot from [link]({link})\n\n"


async def _stream_upload_pipeline(client, source_iter, status_msg, link, task, progress_callback=None):
    """Concurrent download-upload pipeline with immediate cleanup.

    Producer pulls from *source_iter* (blocking, runs in executor).
    Consumer uploads via prepare_media and deletes local file.
    Returns (sent_count, media_list).
    """
    queue = asyncio.Queue(maxsize=2)  # cap memory: at most 2 files buffered
    media = []
    filenames = []
    sent = 0
    uploaded = 0
    loop = asyncio.get_running_loop()
    json_files = []

    async def producer():
        """Pull files from iterator into queue."""
        nonlocal json_files
        def _pull():
            batch = []
            for _ in range(_BATCH_SIZE):
                try:
                    fp = next(source_iter)
                except StopIteration:
                    break
                clean = _sanitize_path(fp)
                if clean.lower().endswith(".json"):
                    json_files.append(clean)
                    continue
                batch.append(clean)
            return batch
        while True:
            batch = await loop.run_in_executor(None, _pull)
            if not batch:
                break
            for fp in batch:
                await queue.put(fp)
        await queue.put(None)  # sentinel

    async def consumer():
        """Upload files from queue and clean up local files."""
        nonlocal uploaded
        while True:
            fp = await queue.get()
            if fp is None:
                break
            try:
                m = await prepare_media(client, fp, progress_callback=progress_callback)
                media.append(m)
                filenames.append(os.path.basename(fp))
                uploaded += 1
                if status_msg and uploaded % 2 == 0:
                    try:
                        await status_msg.edit(
                            f"\U0001f4e4 **SpideyBot:** Uploaded {uploaded} file(s)..."
                        )
                    except TelethonRPCError:
                        pass
            except Exception as e:
                logger.warning("Upload failed", file=fp, error=str(e))
            finally:
                # delete local file after upload (success or fail)
                try:
                    if os.path.isfile(fp):
                        os.remove(fp)
                except OSError:
                    pass

    # Run producer + consumer concurrently
    await asyncio.gather(producer(), consumer())

    # Send all uploaded media as album with per-file captions
    if media:
        captions = [_build_file_caption(fn, link) for fn in filenames]
        try:
            await client.send_file(
                task.event.chat_id,
                media,
                caption=captions,
                reply_to=task.event.message.id,
            )
            sent = len(media)
        except Exception as e:
            logger.warning("Album send failed, falling back to individual", error=str(e))
            for m, fn in zip(media, filenames):
                try:
                    await client.send_file(
                        task.event.chat_id,
                        m,
                        caption=_build_file_caption(fn, link),
                        reply_to=task.event.message.id,
                    )
                    sent += 1
                except Exception as send_err:
                    logger.warning("Individual send failed", error=str(send_err))

    return sent, json_files


async def _send_streaming(
    task, client, dest_dir, source_iter, fallback_downloader,
    status_msg, link, max_size_bytes, progress_callback,
):
    """Concurrent download-upload pipeline. Returns total sent."""
    total_sent = 0
    try:
        total_sent, json_files = await _stream_upload_pipeline(
            client, source_iter, status_msg, link, task,
            progress_callback=progress_callback,
        )
        # Cleanup metadata files
        for fp in json_files:
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except OSError:
                pass
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
                client, task, downloaded, link, status_msg,
                progress_callback=progress_callback,
            )

    # Cleanup staging dir
    try:
        if os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir, ignore_errors=True)
    except Exception:
        pass
    return total_sent


async def _send_all_at_once(client, task, downloaded_files, link, status_msg, progress_callback=None):
    """Upload one-by-one via prepare_media, then send_file as album."""
    media_files = []
    for fp in downloaded_files:
        if os.path.exists(fp) and not fp.lower().endswith(".json"):
            media_files.append(_sanitize_path(fp))

    if not media_files:
        return 0

    media = []
    filenames = []
    for i, fp in enumerate(media_files):
        if not (os.path.exists(fp) and os.path.getsize(fp) > 0):
            continue
        try:
            m = await prepare_media(client, fp, progress_callback=progress_callback)
            media.append(m)
            filenames.append(os.path.basename(fp))
            # Update progress every 2 files
            if status_msg and (i + 1) % 2 == 0:
                try:
                    await status_msg.edit(
                        f"\U0001f4e4 **SpideyBot:** Uploaded {i + 1}/{len(media_files)}..."
                    )
                except TelethonRPCError:
                    pass
        except Exception as e:
            logger.warning("Upload failed", file=fp, error=str(e))
        finally:
            # delete local file after upload
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except OSError:
                pass

    if not media:
        return 0

    captions = [_build_file_caption(fn, link) for fn in filenames]
    try:
        await client.send_file(
            task.event.chat_id,
            media,
            caption=captions,
            reply_to=task.event.message.id,
        )
        return len(media)
    except Exception as e:
        logger.warning("Album send failed, falling back to individual", error=str(e))
        sent = 0
        for m, fn in zip(media, filenames):
            try:
                await client.send_file(
                    task.event.chat_id,
                    m,
                    caption=_build_file_caption(fn, link),
                    reply_to=task.event.message.id,
                )
                sent += 1
            except Exception as send_err:
                logger.warning("Individual send failed", error=str(send_err))
        return sent


async def run_download_task(task, client, fallback_downloader, reddit_downloader=None) -> None:
    """
    Execute a media download task end-to-end.

    Streaming path (site-specific downloaders): files yielded one-by-one,
    batched into groups of 10, sent as albums, cleaned up immediately.
    Gallery-dl path: downloads everything, sends all at once.

    Args:
        task: DownloadTask instance with user info and link.
        client: Telethon TelegramClient instance.
        fallback_downloader: GalleryDLDownloader instance.
    """
    status_msg = task.status_msg
    if not status_msg:
        status_msg = await task.event.reply("\u23f3 **SpideyBot:** Starting download...")

    max_size_bytes, _ = get_size_limit(task.is_premium, task.is_admin)

    _ud = UniversalDownloader()
    _platform = _ud.detect_platform(task.link)
    if _platform != "unknown":
        _site = _platform
    else:
        _site = "gallery-dl"
    current_time_str = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    task_staging_dir = os.path.join(str(task.user_id), _site, str(task.entry_id), current_time_str)
    dest_dir = os.path.join(fallback_downloader.download_dir, task_staging_dir)

    try:
        last_update_time = 0.0

        async def progress_callback(current, total=0):
            nonlocal last_update_time
            now = time.time()
            if now - last_update_time >= 5.0:
                pct = f"{current * 100 // total}%" if total else ""
                try:
                    await status_msg.edit(f"⬆️ Uploading {pct}…")
                    last_update_time = now
                except TelethonRPCError:
                    pass

        async def _edit_status(text: str):
            try:
                await status_msg.edit(text)
            except TelethonRPCError:
                pass

        site_label = _platform if _platform != "unknown" else "gallery-dl"

        use_streaming = _platform != "unknown"

        try:
            await _edit_status(
                f"\U0001f4e5 **SpideyBot:** Downloading from {site_label}..."
            )

            # Wire download progress into site downloader
            if _platform != "unknown":
                _site_dl = _ud.downloaders.get(_platform)
                if _site_dl and hasattr(_site_dl, "_progress_callback"):
                    _loop = asyncio.get_running_loop()

                    def _dl_progress(downloaded, total):
                        pct = downloaded * 100 // total if total else 0
                        mb_done = downloaded / (1024 * 1024)
                        mb_total = total / (1024 * 1024) if total else 0

                        async def _update():
                            try:
                                await status_msg.edit(
                                    f"\U0001f4e5 **SpideyBot:** Downloading… {pct}% "
                                    f"({mb_done:.1f}/{mb_total:.1f} MB)"
                                )
                            except (TelethonRPCError, MessageNotModifiedError):
                                pass

                        _loop.call_soon_threadsafe(asyncio.ensure_future, _update())

                    _site_dl._progress_callback = _dl_progress

            if use_streaming:
                # Streaming path: yield files one-by-one, batch-send
                loop = asyncio.get_running_loop()
                logger.info(
                    "Streaming download via UniversalDownloader",
                    platform=_platform,
                )
                source_iter = await loop.run_in_executor(
                    None,
                    lambda: _ud.download_streaming(task.link, dest_dir),
                )

                total_sent = await _send_streaming(
                    task, client, dest_dir, source_iter, fallback_downloader,
                    status_msg, task.link, max_size_bytes, progress_callback,
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

                total_sent = await _send_all_at_once(
                    client, task, downloaded_files, task.link, status_msg,
                    progress_callback=progress_callback,
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
