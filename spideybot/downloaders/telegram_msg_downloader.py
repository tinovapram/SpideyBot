"""
SpideyBot — Telegram Message Downloader.

Downloads media directly from Telegram message links (t.me).
Supports public channels (t.me/username/12345) and private
channels (t.me/c/1234567890/12345).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import structlog
from telethon import types as tg_types

logger = structlog.get_logger(__name__)


def _tg_media_meta(msg) -> dict | None:
    """Detect the media type of a Telegram message for re-upload.

    Returns a dict with keys suitable for ``_determine_tg_flags``:
        photo, animated, video, force_document
    """
    media = msg.media
    if media is None:
        return None
    if isinstance(media, tg_types.MessageMediaPhoto):
        return {"photo": True}
    doc = getattr(media, "document", None)
    if doc is None:
        return None
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, tg_types.DocumentAttributeAnimated):
            return {"animated": True}
        if isinstance(attr, tg_types.DocumentAttributeVideo):
            return {"video": True}
    return {"force_document": True}


# Patterns for Telegram message links
# Public:  https://t.me/username/12345
# Private: https://t.me/c/1234567890/12345
_TG_LINK_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/(?:c/(\d+)|(\w+))/(\d+)",
    re.IGNORECASE,
)


@dataclass
class TGLink:
    """Parsed Telegram message link."""
    chat_id: int       # resolved chat entity (int for private, str for public)
    message_id: int
    is_private: bool
    raw_chat: str      # raw channel ID or username


# Pattern for range format: url1-url2 (two t.me links separated by dash)
_TG_RANGE_RE = re.compile(
    r"(https?://(?:t\.me|telegram\.me)/(?:c/\d+|\w+)/\d+)\s*-\s*(https?://(?:t\.me|telegram\.me)/(?:c/\d+|\w+)/\d+)",
    re.IGNORECASE,
)


def is_tg_range_url(url: str) -> bool:
    """Check if the URL is a Telegram message range (url1-url2)."""
    return bool(_TG_RANGE_RE.fullmatch(url.strip()))


def parse_tg_link(url: str) -> TGLink:
    """
    Parse a Telegram message link.

    Raises:
        ValueError: if the URL is not a valid Telegram message link.
    """
    m = _TG_LINK_RE.search(url)
    if not m:
        raise ValueError(f"Not a valid Telegram message link: {url}")

    private_id, username, msg_id = m.group(1), m.group(2), m.group(3)

    if private_id:
        # Private channel: -100{id}
        chat_id = int(f"-100{private_id}")
        return TGLink(chat_id=chat_id, message_id=int(msg_id), is_private=True, raw_chat=private_id)
    else:
        # Public channel: use username string
        return TGLink(chat_id=username, message_id=int(msg_id), is_private=False, raw_chat=username)


def parse_tg_range(url: str) -> tuple[TGLink, TGLink]:
    """
    Parse a Telegram message range link (url1-url2).

    Both URLs must reference the same chat.
    Returns (link_start, link_end) where message IDs form the range.

    Raises:
        ValueError: if the URL is not a valid range or chats don't match.
    """
    m = _TG_RANGE_RE.fullmatch(url.strip())
    if not m:
        raise ValueError(f"Not a valid Telegram message range: {url}")

    link1 = parse_tg_link(m.group(1))
    link2 = parse_tg_link(m.group(2))

    # Ensure both URLs refer to the same chat
    if link1.raw_chat != link2.raw_chat:
        raise ValueError(
            f"Range URLs must reference the same channel: "
            f"'{link1.raw_chat}' != '{link2.raw_chat}'"
        )

    # Normalize order so link_start has the smaller message ID
    if link1.message_id <= link2.message_id:
        return link1, link2
    return link2, link1


async def download_tg_message(client, url: str, output_dir: str = "./downloads/tg") -> dict:
    """
    Download all media from a Telegram message link using the given client.

    Args:
        client: Telethon TelegramClient (user's client).
        url: Telegram message link.
        output_dir: Directory to save downloaded files.

    Returns:
        dict with keys:
            ok: bool
            error: str (if ok=False)
            files: list[str] — downloaded file paths
            caption: str — message text/caption
            chat_title: str
    """
    try:
        link = parse_tg_link(url)
    except ValueError as e:
        return {"ok": False, "error": str(e), "files": [], "caption": "", "chat_title": ""}

    try:
        entity = await client.get_entity(link.chat_id)
    except Exception as e:
        return {
            "ok": False,
            "error": f"Cannot access chat `{link.raw_chat}`: {e}",
            "files": [],
            "caption": "",
            "chat_title": "",
        }

    try:
        message = await client.get_messages(entity, ids=link.message_id)
    except Exception as e:
        return {
            "ok": False,
            "error": f"Cannot fetch message #{link.message_id}: {e}",
            "files": [],
            "caption": "",
            "chat_title": "",
        }

    if message is None:
        return {
            "ok": False,
            "error": f"Message #{link.message_id} not found (may be deleted or inaccessible).",
            "files": [],
            "caption": "",
            "chat_title": "",
        }

    # Get chat title
    chat_title = getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")

    # Get caption/text
    caption = message.text or ""

    # Collect media files
    media = message.media
    if media is None:
        return {
            "ok": False,
            "error": "Message has no media (text-only message).",
            "files": [],
            "caption": caption,
            "chat_title": chat_title,
        }

    os.makedirs(output_dir, exist_ok=True)

    downloaded_files = []
    file_metadata = []

    # Handle grouped media (albums)
    if hasattr(message, "grouped_id") and message.grouped_id:
        # Fetch all messages in the group
        messages = await client.get_messages(
            entity,
            ids=list(range(link.message_id - 10, link.message_id + 10)),
        )
        group_messages = [m for m in messages if m and m.grouped_id == message.grouped_id]
        group_messages.sort(key=lambda m: m.id)
    else:
        group_messages = [message]

    for msg in group_messages:
        if msg.media is None:
            continue
        meta = _tg_media_meta(msg)
        try:
            path = await client.download_media(msg, file=output_dir)
            if path:
                downloaded_files.append(path)
                file_metadata.append(meta)
                logger.info("Downloaded TG media", chat=chat_title, msg_id=msg.id, file=path)
        except Exception as e:
            logger.error("Failed to download TG media", msg_id=msg.id, error=str(e))

    if not downloaded_files:
        return {
            "ok": False,
            "error": "No downloadable media found in this message.",
            "files": [],
            "file_metadata": [],
            "caption": caption,
            "chat_title": chat_title,
        }

    return {
        "ok": True,
        "error": "",
        "files": downloaded_files,
        "file_metadata": file_metadata,
        "captions": [caption] if caption else [],
        "caption": caption,
        "chat_title": chat_title,
    }


async def download_tg_range(client, url: str, output_dir: str = "./downloads/tg") -> dict:
    """
    Download all media from a range of Telegram messages (url1-url2).

    Args:
        client: Telethon TelegramClient (user's client).
        url: Telegram message range link (e.g. ``https://t.me/c/X/5-https://t.me/c/X/54``).
        output_dir: Directory to save downloaded files.

    Returns:
        dict with keys:
            ok: bool
            error: str (if ok=False)
            files: list[str] — downloaded file paths
            caption: str — concatenated text from all messages
            chat_title: str
            total_messages: int — number of messages in the range
            downloaded_messages: int — number of messages that had media
    """
    try:
        link_start, link_end = parse_tg_range(url)
    except ValueError as e:
        return {
            "ok": False, "error": str(e),
            "files": [], "caption": "", "chat_title": "",
            "total_messages": 0, "downloaded_messages": 0,
        }

    try:
        entity = await client.get_entity(link_start.chat_id)
    except Exception as e:
        return {
            "ok": False,
            "error": f"Cannot access chat `{link_start.raw_chat}`: {e}",
            "files": [], "caption": "", "chat_title": "",
            "total_messages": 0, "downloaded_messages": 0,
        }

    chat_title = getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")

    # Fetch all messages in the range (inclusive)
    start_id = link_start.message_id
    end_id = link_end.message_id
    msg_ids = list(range(start_id, end_id + 1))

    logger.info(
        "Fetching TG range",
        chat=chat_title, start=start_id, end=end_id,
        count=len(msg_ids),
    )

    try:
        # Telethon supports batch fetching with a list of IDs
        messages = await client.get_messages(entity, ids=msg_ids)
    except Exception as e:
        return {
            "ok": False,
            "error": f"Cannot fetch messages {start_id}-{end_id}: {e}",
            "files": [], "caption": "", "chat_title": "",
            "total_messages": 0, "downloaded_messages": 0,
        }

    # Filter out None (deleted/inaccessible) messages, sort by ID
    valid_messages = sorted(
        [m for m in messages if m is not None],
        key=lambda m: m.id,
    )

    if not valid_messages:
        return {
            "ok": False,
            "error": f"No messages found in range {start_id}-{end_id}.",
            "files": [], "caption": "", "chat_title": chat_title,
            "total_messages": len(msg_ids), "downloaded_messages": 0,
        }

    os.makedirs(output_dir, exist_ok=True)
    downloaded_files = []
    file_metadata = []
    captions = []

    for msg in valid_messages:
        # Track caption text (non-empty messages)
        if msg.media is None:
            continue

        # Handle grouped media (albums) — skip duplicates
        if hasattr(msg, "grouped_id") and msg.grouped_id:
            # Only download the first message of each group to avoid duplicates
            # Check if a previous message in our set belongs to the same group
            is_first_in_group = True
            for prev_msg in valid_messages:
                if prev_msg.id >= msg.id:
                    break
                if prev_msg.grouped_id == msg.grouped_id:
                    is_first_in_group = False
                    break

            if not is_first_in_group:
                continue

            # Fetch all messages in this group
            group_msgs = [
                m for m in valid_messages if m.grouped_id == msg.grouped_id
            ]
            for gmsg in group_msgs:
                if gmsg.media is None:
                    continue
                meta = _tg_media_meta(gmsg)
                try:
                    text = gmsg.text or ""
                    captions.append(text)
                    path = await client.download_media(gmsg, file=output_dir)
                    if path:
                        downloaded_files.append(path)
                        file_metadata.append(meta)
                        logger.info("Downloaded TG range media", msg_id=gmsg.id, file=path)
                except Exception as e:
                    logger.error("Failed to download TG range media", msg_id=gmsg.id, error=str(e))
        else:
            meta = _tg_media_meta(msg)
            text = msg.text or ""
            if text:
                captions.append(text)
            try:
                path = await client.download_media(msg, file=output_dir)
                if path:
                    downloaded_files.append(path)
                    file_metadata.append(meta)
                    logger.info("Downloaded TG range media", msg_id=msg.id, file=path)
            except Exception as e:
                logger.error("Failed to download TG range media", msg_id=msg.id, error=str(e))

    caption_text = "\n\n".join(captions) if captions else ""

    if not downloaded_files:
        return {
            "ok": False,
            "error": f"No downloadable media found in range {start_id}-{end_id}.",
            "files": [], "file_metadata": [], "caption": caption_text, "chat_title": chat_title,
            "total_messages": len(valid_messages), "downloaded_messages": 0,
        }

    return {
        "ok": True,
        "error": "",
        "files": downloaded_files,
        "file_metadata": file_metadata,
        "captions": captions,
        "caption": caption_text,
        "chat_title": chat_title,
        "total_messages": len(valid_messages),
        "downloaded_messages": len(downloaded_files),
    }
