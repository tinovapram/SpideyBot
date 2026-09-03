"""
SpideyBot — File Utilities.

Filename sanitization, post text/caption extraction from metadata JSON,
async file download helper, and media preparation with thumbnail generation.
"""

import os
import re
import json
import asyncio
import subprocess
import aiohttp
from typing import Optional, List

import structlog

logger = structlog.get_logger(__name__)


def sanitize_filename(filename: str, max_len: int = 120) -> str:
    """
    Sanitize filename to comply with both Windows and Linux naming rules,
    and truncate to prevent path length errors.

    Args:
        filename: Original filename string.
        max_len: Maximum length for the name portion (excluding extension).

    Returns:
        A safe, truncated filename string.
    """
    # Split filename and extension
    name, ext = os.path.splitext(filename)

    # Replace invalid chars with underscore
    clean_name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name)
    # Strip leading/trailing spaces and periods
    clean_name = clean_name.strip(" .")

    # Truncate clean_name if too long (reserving space for extension)
    if len(clean_name) > max_len:
        clean_name = clean_name[:max_len].strip(" .")

    if not clean_name:
        clean_name = "file"

    # Re-assemble filename
    clean_ext = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", ext).strip(" .")
    if len(clean_ext) > 10:
        clean_ext = clean_ext[:10]

    return f"{clean_name}.{clean_ext}" if clean_ext else clean_name


def extract_post_text(json_paths: List[str]) -> Optional[str]:
    """
    Extract a post description/caption from gallery-dl metadata JSON files.

    Searches for common text content keys (content, description, caption, etc.)
    across all provided JSON files and returns the best match.

    Args:
        json_paths: List of paths to JSON metadata files.

    Returns:
        The extracted text (truncated to 1024 chars), or None if not found.
    """
    # Target keys to search for, in lowercase
    target_keys = {
        'content', 'description', 'caption', 'title', 'text',
        'desc', 'selftext', 'tweet_text', 'message'
    }

    # Store matches: {key: content}
    found_texts = {}

    def search_recursive(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_lower = k.lower()
                if k_lower == 'category':
                    if isinstance(v, str) and v.strip():
                        if k_lower not in found_texts:
                            found_texts[k_lower] = v.strip()
                elif k_lower == 'author':
                    if isinstance(v, str) and v.strip():
                        if k_lower not in found_texts:
                            found_texts[k_lower] = v.strip()
                    elif isinstance(v,dict):
                        if k_lower not in found_texts:
                            found_texts[k_lower] = v['name'].strip()
                elif k_lower in target_keys:
                    if isinstance(v, str) and v.strip():
                        if k_lower not in found_texts:
                            found_texts[k_lower] = v.strip()
                    elif isinstance(v, dict):
                        # Some structures have nested dicts wrapping the text
                        for sub_k in ['text', 'value', 'content']:
                            sub_v = v.get(sub_k)
                            if sub_v and isinstance(sub_v, str) and sub_v.strip():
                                if k_lower not in found_texts:
                                    found_texts[k_lower] = sub_v.strip()
                                    break
                # Recursively search children
                search_recursive(v)
        elif isinstance(obj, list):
            for item in obj:
                search_recursive(item)

    for jp in json_paths:
        try:
            with open(jp, 'r', encoding='utf-8') as f:
                data = json.load(f)
                search_recursive(data)
        except (json.JSONDecodeError, OSError):
            logger.debug(f"Failed to read metadata JSON: {jp}")

    # Priority order for selecting the best caption key
    priority_order = [
        'content', 'description', 'caption', 'text', 'desc',
        'selftext', 'tweet_text', 'title', 'message'
    ]
    caption = ''
    category = found_texts.get('category', '')
    author = found_texts.get('author', 'unknown')
    if category == 'reddit':
        caption = found_texts.get('title', '')[:1024]
    else:
        for pk in priority_order:
            if pk in found_texts:
                caption = found_texts[pk][:1024]
                break
    if caption:
        caption = f"{author} on {category}:\n\n{caption}\n"
        return caption
    return None


async def download_file_async(terabox_downloader, tb_file, output_dir: str) -> str:
    """
    Download a TeraBox file asynchronously using aiohttp streaming.

    Uses adaptive per-read timeouts scaled to file size so large videos
    don't fail on slow connections.  Partial files are cleaned up on error.

    Args:
        terabox_downloader: TeraBoxDownloader instance.
        tb_file: TeraBoxFile with a valid dlink.
        output_dir: Directory to save the downloaded file.

    Returns:
        The path to the downloaded file.

    Raises:
        Exception: On network, timeout, or I/O errors. The partial file
                    is removed before the exception propagates.
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_filename = sanitize_filename(tb_file.filename)
    filepath = os.path.join(output_dir, safe_filename)

    # ── Adaptive timeout based on file size ────────────────────────
    # sock_read is per-chunk read.  TeraBox throttles large files,
    # so small fixed values fail on slow connections.  Scale up.
    size_mb = (tb_file.size_bytes or 0) / (1024 * 1024)

    if size_mb < 50:
        sock_read = 120        # small files: 2 min per read
    elif size_mb < 500:
        sock_read = 300        # medium: 5 min per read
    elif size_mb < 2048:
        sock_read = 600        # large (2 GB): 10 min per read
    else:
        sock_read = 900        # huge (>2 GB): 15 min per read

    # total = 0 (uncapped) — let sock_read guard individual stalls
    dl_timeout = aiohttp.ClientTimeout(total=0, connect=30, sock_read=sock_read)

    logger.debug(
        "Download timeout set",
        filename=tb_file.filename,
        size_mb=f"{size_mb:.1f}",
        sock_read=sock_read,
    )

    r = await terabox_downloader._request_with_retry(
        "GET",
        tb_file.dlink,
        headers={"User-Agent": terabox_downloader.USER_AGENT},
        timeout=dl_timeout,
    )
    try:
        with open(filepath, "wb") as f:
            async for chunk in r.content.iter_chunked(8192):
                f.write(chunk)
    except (asyncio.CancelledError, Exception):
        # Clean up partial file on any failure (timeout, I/O, cancel)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except OSError:
            pass
        raise
    finally:
        await r.release()
    return filepath


# ── Thumbnail + media preparation for uploads ────────────────────

_VIDEO_EXTS = frozenset({'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv', '.m4v'})
_BIG_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


def make_video_thumb(video_path: str) -> Optional[str]:
    """Extract first frame as JPEG thumbnail for videos >10 MB.

    Returns the thumb file path, or None if no thumb is needed/possible.
    Caller cleans up the returned file after uploading.
    """
    ext = os.path.splitext(video_path)[1].lower()
    if ext not in _VIDEO_EXTS:
        return None
    try:
        if os.path.getsize(video_path) <= _BIG_FILE_BYTES:
            return None
    except OSError:
        return None

    thumb_path = video_path + ".thumb.jpg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-ss", "00:00:01", "-vframes", "1",
                "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
                "-q:v", "5", thumb_path,
            ],
            capture_output=True, timeout=30, check=True,
        )
        if os.path.exists(thumb_path):
            logger.debug("Thumbnail generated", video=video_path, thumb=thumb_path)
            return thumb_path
    except Exception:
        pass
    # cleanup failed thumb
    try:
        os.remove(thumb_path)
    except OSError:
        pass
    return None


async def prepare_media(
    client, file_path: str, *,
    thumb: Optional[str] = None,
    as_image: Optional[bool] = None,
    force_document: bool = False,
    supports_streaming: Optional[bool] = None,
    nosound_video: Optional[bool] = None,
    progress_callback=None
):
    """Upload a file and return an InputMedia object with auto-thumbnail.

    Our own ``_file_to_media``: uploads the file, generates + uploads a
    JPEG thumbnail for videos exceeding 10 MB, and returns an
    ``InputMediaUploadedDocument`` (or ``InputMediaUploadedPhoto`` for
    images).  The result is safe to pass directly to ``send_file`` or
    build into an album list — Telethon treats pre-built InputMedia as
    pass-through via ``get_input_media``.

    Keyword-only params:
        thumb:             Custom thumb path (skips auto-generation).
        as_image:          Force photo treatment (None = auto-detect).
        force_document:    Force document even for images.
        supports_streaming: Override streaming flag (None = video-only).
        nosound_video:     True prevents silent videos being sent as GIF.
    """
    from telethon import types
    from telethon.utils import get_attributes, is_image

    file_handle = await client.upload_file(file_path, progress_callback=progress_callback,)

    _is_image = is_image(file_path)
    if as_image is None:
        as_image = _is_image and not force_document

    if as_image:
        return types.InputMediaUploadedPhoto(file=file_handle)

    is_video = os.path.splitext(file_path)[1].lower() in _VIDEO_EXTS
    if supports_streaming is None:
        supports_streaming = is_video
    
    attrs, mime = get_attributes(file_path, force_document=force_document, supports_streaming=supports_streaming)

    # Thumbnail: caller-supplied > auto-generated > none
    thumb_handle = None
    if thumb:
        if not os.path.isfile(thumb):
            logger.warning("Thumb path not found, skipping", thumb=thumb)
        else:
            try:
                thumb_handle = await client.upload_file(thumb)
            except Exception:
                thumb_handle = None
    else:
        auto_thumb = make_video_thumb(file_path)
        if auto_thumb:
            try:
                thumb_handle = await client.upload_file(auto_thumb)
            except Exception:
                thumb_handle = None
            finally:
                try:
                    os.remove(auto_thumb)
                except OSError:
                    pass

  
    # nosound_video: only relevant for video mime, else None
    ns_video = ns_video = (nosound_video if nosound_video is not None else True) if mime.startswith('video') else None

    return types.InputMediaUploadedDocument(
        file=file_handle,
        mime_type=mime,
        attributes=attrs,
        thumb=thumb_handle,
        force_file=force_document and not _is_image,
        nosound_video=ns_video,
    )


async def prepare_media_batch(client, file_paths, progress_callback=None, **kwargs):
    """Upload a list of files and return InputMedia objects for ``send_file``.

    Filters out 0-byte / missing files.  If *progress_callback* is
    provided it receives ``(completed, total)`` after each file upload.
    Extra *kwargs* are forwarded to :func:`prepare_media`.

    Returns a list of ``InputMediaUploadedDocument`` / ``InputMediaUploadedPhoto``.
    """
    valid = [fp for fp in file_paths if os.path.exists(fp) and os.path.getsize(fp) > 0]
    media = []
    for i, fp in enumerate(valid):
        try:
            m = await prepare_media(client, fp, **kwargs)
            media.append(m)
        except Exception as e:
            logger.warning("Failed to prepare media", file=fp, error=str(e))
        if progress_callback:
            try:
                progress_callback(i + 1, len(valid))
            except Exception:
                pass
    return media
