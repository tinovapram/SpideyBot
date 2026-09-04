"""
File utilities: filename sanitization, metadata extraction, and media
preparation for Telegram uploads.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

_VIDEO_EXTS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v"})
_BIG_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

_CAPTION_KEYS = {
    "content", "description", "caption", "title", "text",
    "desc", "selftext", "tweet_text", "message",
}
_CAPTION_PRIORITY = [
    "content", "description", "caption", "text", "desc",
    "selftext", "tweet_text", "title", "message",
]


def sanitize_filename(filename: str, max_len: int = 120) -> str:
    """Return a filename safe for Windows and Linux, truncated if needed."""
    name, ext = os.path.splitext(filename)

    clean_name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name).strip(" .")
    if len(clean_name) > max_len:
        clean_name = clean_name[:max_len].strip(" .")
    if not clean_name:
        clean_name = "file"

    clean_ext = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", ext).strip(" .")[:10]

    return f"{clean_name}.{clean_ext}" if clean_ext else clean_name


def build_caption(filename: str, link: str) -> str:
    """Build a per-file caption with the filename and a download footer."""
    return f"{filename}\n\nDownloaded by SpideyBot from [link]({link})\n\n"


def extract_post_text(json_paths: list[str]) -> Optional[str]:
    """Extract a caption from gallery-dl metadata JSON files, or None."""
    found: dict[str, str] = {}

    def _search(obj) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_lower = key.lower()
                if key_lower == "category" and isinstance(value, str) and value.strip():
                    found.setdefault("category", value.strip())
                elif key_lower == "author":
                    if isinstance(value, str) and value.strip():
                        found.setdefault("author", value.strip())
                    elif isinstance(value, dict):
                        found.setdefault("author", str(value.get("name", "")).strip())
                elif key_lower in _CAPTION_KEYS and isinstance(value, str) and value.strip():
                    found.setdefault(key_lower, value.strip())
                elif key_lower in _CAPTION_KEYS and isinstance(value, dict):
                    for sub_key in ("text", "value", "content"):
                        sub = value.get(sub_key)
                        if isinstance(sub, str) and sub.strip():
                            found.setdefault(key_lower, sub.strip())
                            break
                _search(value)
        elif isinstance(obj, list):
            for item in obj:
                _search(item)

    for path in json_paths:
        try:
            with open(path, encoding="utf-8") as handle:
                _search(json.load(handle))
        except (json.JSONDecodeError, OSError):
            logger.debug("Failed to read metadata JSON", path=path)

    category = found.get("category", "")
    author = found.get("author", "unknown")

    if category == "reddit":
        caption = found.get("title", "")[:1024]
    else:
        caption = ""
        for key in _CAPTION_PRIORITY:
            if key in found:
                caption = found[key][:1024]
                break

    return f"{author} on {category}:\n\n{caption}\n" if caption else None


def make_video_thumb(video_path: str) -> Optional[str]:
    """Extract the first frame as a JPEG thumbnail for large videos."""
    ext = os.path.splitext(video_path)[1].lower()
    if ext not in _VIDEO_EXTS:
        return None
    try:
        if os.path.getsize(video_path) <= _BIG_FILE_BYTES:
            return None
    except OSError:
        return None

    thumb_path = f"{video_path}.thumb.jpg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-ss", "00:00:01", "-vframes", "1",
                "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
                "-q:v", "5", thumb_path,
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        if os.path.exists(thumb_path):
            return thumb_path
    except Exception:
        pass
    try:
        os.remove(thumb_path)
    except OSError:
        pass
    return None


async def prepare_media(
    client,
    file_path: str,
    *,
    thumb: Optional[str] = None,
    as_image: Optional[bool] = None,
    force_document: bool = False,
    supports_streaming: Optional[bool] = None,
    nosound_video: Optional[bool] = None,
    progress_callback=None,
):
    """Upload *file_path* and return a Telethon ``InputMedia`` object."""
    from telethon import types
    from telethon.utils import get_attributes, is_image

    file_handle = await client.upload_file(file_path, progress_callback=progress_callback)

    is_img = is_image(file_path)
    if as_image is None:
        as_image = is_img and not force_document

    if as_image:
        return types.InputMediaUploadedPhoto(file=file_handle)

    is_video = os.path.splitext(file_path)[1].lower() in _VIDEO_EXTS
    if supports_streaming is None:
        supports_streaming = is_video

    attrs, mime = get_attributes(
        file_path,
        force_document=force_document,
        supports_streaming=supports_streaming,
    )

    thumb_handle = None
    if thumb and os.path.isfile(thumb):
        try:
            thumb_handle = await client.upload_file(thumb)
        except Exception:
            thumb_handle = None
    elif thumb is None:
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

    nosound = (nosound_video if nosound_video is not None else True) if mime.startswith("video") else None

    return types.InputMediaUploadedDocument(
        file=file_handle,
        mime_type=mime,
        attributes=attrs,
        thumb=thumb_handle,
        force_file=force_document and not is_img,
        nosound_video=nosound,
    )


async def prepare_media_batch(client, file_paths, progress_callback=None, **kwargs):
    """Upload a list of files and return a list of ``InputMedia`` objects."""
    valid = [fp for fp in file_paths if os.path.exists(fp) and os.path.getsize(fp) > 0]
    media = []
    for index, fp in enumerate(valid):
        try:
            media.append(await prepare_media(client, fp, **kwargs))
        except Exception as exc:
            logger.warning("Failed to prepare media", file=fp, error=str(exc))
        if progress_callback:
            try:
                progress_callback(index + 1, len(valid))
            except Exception:
                pass
    return media
