"""
Generic download orchestration for non-TeraBox links.

Two paths:
- **Streaming** — a site downloader yields files one by one; each is uploaded
  immediately and flushed to Telegram in albums of 10 (bounded memory/disk).
- **gallery-dl fallback** — downloads everything at once, then sends all.

All phases (site-download bytes, upload bytes/counts) report through one
unified :class:`~utils.progress.StatusMessage` bound to the status message.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time

import structlog

from core import config
from downloader.registry import get_registry
from utils import paths
from utils.files import build_caption, extract_native_text, prepare_media, sanitize_filename
from utils.progress import StatusMessage, count_line
from utils.telethon import send_album

logger = structlog.get_logger(__name__)

_ALBUM_LIMIT = 10
_QUEUE_SIZE = 4


async def run_download(task, client) -> None:
    """Execute a generic (non-TeraBox) download task end-to-end."""
    footer = f"Send `/cancel {task.entry_id}` to abort."
    if task.status_msg is not None:
        status = StatusMessage(task.status_msg, footer=footer)
    else:
        status = StatusMessage(
            await task.event.reply("⏳ **SpideyBot:** Starting download..."), footer=footer
        )
    max_size_bytes, _ = config.get_size_limit(task.is_premium, task.is_admin)

    detected = get_registry().detect(task.link)
    site_label = detected[0] if detected else "gallery-dl"

    status.set_header(f"📥 **SpideyBot:** Downloading from {site_label}")

    try:
        if detected is not None:
            sent = await _run_streaming(task, client, detected[0], detected[1], status, max_size_bytes)
        else:
            sent = await _run_gallerydl(task, client, status, max_size_bytes)
    except Exception as exc:
        logger.exception("Download failed", link=task.link, error=str(exc))
        await status.close(f"❌ **SpideyBot: Failed to download media.**\nReason: `{exc}`")
        return

    if sent > 0:
        await status.close(f"✅ **SpideyBot: Done!** Sent {sent} file(s).")
    else:
        await status.close("❌ **SpideyBot:** No files were successfully sent.")


def _staging_dir(task, site: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return str(paths.DOWNLOADS_DIR / str(task.user_id) / site / str(task.entry_id) / stamp)


async def _run_streaming(task, client, site, downloader, status, max_size_bytes) -> int:
    dest_dir = _staging_dir(task, site)
    loop = asyncio.get_running_loop()
    downloader._progress_callback = status.bytes_cb("dl", "📥", "Downloading")

    try:
        source = await loop.run_in_executor(
            None, lambda: downloader.download_streaming(task.link, dest_dir)
        )
        sent = await _stream_upload(task, client, source, status)
    except Exception as exc:
        logger.warning("Streaming download failed, falling back to gallery-dl", error=str(exc))
        status.set_header("⚠️ **SpideyBot:** Primary download failed, trying gallery-dl")
        status.drop("dl")
        status.drop("dl_count")
        from downloader.gallerydl import GalleryDLDownloader

        fallback = GalleryDLDownloader()
        downloaded = await fallback.download(
            task.link, _staging_dir(task, "gdl_fallback"), max_size_bytes,
            progress_callback=await _gallerydl_hook(status),
        )
        sent = await _send_all_at_once(task, client, downloaded, status) if downloaded else 0
    finally:
        downloader._progress_callback = None
        shutil.rmtree(dest_dir, ignore_errors=True)

    return sent


async def _run_gallerydl(task, client, status, max_size_bytes) -> int:
    from downloader.gallerydl import GalleryDLDownloader

    fallback = GalleryDLDownloader()
    staging = _staging_dir(task, "gallery-dl")

    downloaded = await fallback.download(
        task.link, staging, max_size_bytes,
        progress_callback=await _gallerydl_hook(status),
    )
    if not downloaded:
        await status.close("❌ **SpideyBot: No files downloaded from the link.**")
        return 0

    sent = await _send_all_at_once(task, client, downloaded, status)
    shutil.rmtree(staging, ignore_errors=True)
    return sent


async def _gallerydl_hook(status: StatusMessage):
    """Wrap gallery-dl's async text progress into a ``StatusMessage`` row."""
    async def hook(text: str) -> None:
        status.row("gdl", text)
    return hook


async def _stream_upload(task, client, source, status) -> int:
    """Producer/consumer pipeline: download → upload → album (bounded memory)."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
    loop = asyncio.get_running_loop()
    sent = 0
    uploaded = 0
    upload_cb = status.bytes_cb("ul", "📤", "Uploading")
    # Shared state: producer writes downloaded count, consumer reads it.
    state = {"downloaded": 0, "total": None}

    async def produce() -> None:
        def pull():
            try:
                return next(source)
            except StopIteration:
                return None

        while True:
            file_path = await loop.run_in_executor(None, pull)
            if file_path is None:
                break
            state["downloaded"] += 1
            status.row("dl_count", count_line("📥", "Downloaded", state["downloaded"]))
            await queue.put(file_path)
        state["total"] = state["downloaded"]
        # Switch to final form with total
        status.row("dl_count", count_line("📥", "Downloaded", state["downloaded"], state["total"]))
        await queue.put(None)

    async def consume() -> None:
        nonlocal sent, uploaded
        pending_media: list = []
        pending_captions: list[str] = []
        # Native post caption captured from a metadata sidecar; applied to the
        # first media of this download so ``filename + Downloaded by`` stays intact.
        native_text: str | None = None
        native_used = False

        async def flush() -> None:
            nonlocal sent
            if not pending_media:
                return
            media, captions = pending_media[:], pending_captions[:]
            pending_media.clear()
            pending_captions.clear()
            status.drop("ul")
            sent += await send_album(
                client, task.event.chat_id, media, captions,
                reply_to=task.event.message.id,
            )

        while True:
            file_path = await queue.get()
            if file_path is None:
                break

            clean = _sanitize_path(file_path)
            if clean.lower().endswith(".json"):
                if native_text is None:
                    native_text = extract_native_text([clean])
                _remove(clean)
                continue

            try:
                media = await prepare_media(client, clean, progress_callback=upload_cb)
                pending_media.append(media)
                native = native_text if (native_text and not native_used) else None
                if native:
                    native_used = True
                pending_captions.append(
                    build_caption(os.path.basename(clean), task.link, native=native)
                )
                uploaded += 1
                total = state.get("total")
                status.row("ul", count_line("📤", "Uploaded", uploaded, total))
                if len(pending_media) >= _ALBUM_LIMIT:
                    await flush()
            except Exception as exc:
                logger.warning("Upload failed", file=clean, error=str(exc))
            finally:
                _remove(clean)

        await flush()

    await asyncio.gather(produce(), consume())
    return sent


async def _send_all_at_once(task, client, downloaded_files, status) -> int:
    json_paths = [
        fp for fp in downloaded_files
        if os.path.exists(fp) and fp.lower().endswith(".json")
    ]
    native_text = extract_native_text(json_paths)

    media_files = [
        _sanitize_path(fp)
        for fp in downloaded_files
        if os.path.exists(fp) and os.path.getsize(fp) > 0 and not fp.lower().endswith(".json")
    ]
    if not media_files:
        return 0

    media, captions = [], []
    upload_cb = status.bytes_cb("ul", "📤", "Uploading")
    uploaded = 0
    native_used = False
    for fp in media_files:
        try:
            media.append(await prepare_media(client, fp, progress_callback=upload_cb))
            native = native_text if (native_text and not native_used) else None
            if native:
                native_used = True
            captions.append(build_caption(os.path.basename(fp), task.link, native=native))
            uploaded += 1
            status.row("up", count_line("📤", "Uploaded", uploaded))
        except Exception as exc:
            logger.warning("Upload failed", file=fp, error=str(exc))
        finally:
            _remove(fp)

    status.drop("ul")
    if not media:
        return 0

    return await send_album(
        client, task.event.chat_id, media, captions,
        reply_to=task.event.message.id,
    )


def _sanitize_path(file_path: str) -> str:
    """Rename a file on disk if its filename needs sanitization."""
    if not os.path.exists(file_path):
        return file_path

    directory, filename = os.path.split(file_path)
    clean_name = sanitize_filename(filename)
    if clean_name == filename:
        return file_path

    clean_path = os.path.join(directory, clean_name)
    try:
        if os.path.exists(clean_path) and clean_path != file_path:
            base, ext = os.path.splitext(clean_name)
            counter = 1
            while os.path.exists(os.path.join(directory, f"{base}_{counter}{ext}")):
                counter += 1
            clean_path = os.path.join(directory, f"{base}_{counter}{ext}")
        os.rename(file_path, clean_path)
        return clean_path
    except Exception as exc:
        logger.warning("Failed to rename file", old=file_path, new=clean_path, error=str(exc))
        return file_path


def _remove(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
