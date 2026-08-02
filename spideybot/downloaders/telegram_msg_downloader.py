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

logger = structlog.get_logger(__name__)

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
        try:
            path = await client.download_media(msg, file=output_dir)
            if path:
                downloaded_files.append(path)
                logger.info("Downloaded TG media", chat=chat_title, msg_id=msg.id, file=path)
        except Exception as e:
            logger.error("Failed to download TG media", msg_id=msg.id, error=str(e))

    if not downloaded_files:
        return {
            "ok": False,
            "error": "No downloadable media found in this message.",
            "files": [],
            "caption": caption,
            "chat_title": chat_title,
        }

    return {
        "ok": True,
        "error": "",
        "files": downloaded_files,
        "caption": caption,
        "chat_title": chat_title,
    }
