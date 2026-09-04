"""
Telegram message media downloader.

Downloads media from ``t.me`` message links (single message or a range),
supporting public channels (``t.me/username/123``) and private channels
(``t.me/c/1234567890/123``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import structlog
from telethon import types as tg_types

logger = structlog.get_logger(__name__)

_TG_LINK_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/(?:c/(\d+)|(\w+))/(\d+)", re.IGNORECASE
)
_TG_RANGE_RE = re.compile(
    r"(https?://(?:t\.me|telegram\.me)/(?:c/\d+|\w+)/\d+)\s*-\s*"
    r"(https?://(?:t\.me|telegram\.me)/(?:c/\d+|\w+)/\d+)",
    re.IGNORECASE,
)


def _tg_media_meta(message) -> dict | None:
    """Detect the media type of a Telegram message for re-upload."""
    media = message.media
    if media is None:
        return None
    if isinstance(media, tg_types.MessageMediaPhoto):
        return {"photo": True}

    document = getattr(media, "document", None)
    if document is None:
        return None

    for attribute in getattr(document, "attributes", []):
        if isinstance(attribute, tg_types.DocumentAttributeAnimated):
            return {"animated": True}
        if isinstance(attribute, tg_types.DocumentAttributeVideo):
            return {"video": True}
    return {"force_document": True}


@dataclass
class TGLink:
    """Parsed Telegram message link."""

    chat_id: int | str
    message_id: int
    is_private: bool
    raw_chat: str


def is_tg_range_url(url: str) -> bool:
    return bool(_TG_RANGE_RE.fullmatch(url.strip()))


def parse_tg_link(url: str) -> TGLink:
    match = _TG_LINK_RE.search(url)
    if not match:
        raise ValueError(f"Not a valid Telegram message link: {url}")

    private_id, username, msg_id = match.groups()
    if private_id:
        return TGLink(
            chat_id=int(f"-100{private_id}"),
            message_id=int(msg_id),
            is_private=True,
            raw_chat=private_id,
        )
    return TGLink(chat_id=username, message_id=int(msg_id), is_private=False, raw_chat=username)


def parse_tg_range(url: str) -> tuple[TGLink, TGLink]:
    match = _TG_RANGE_RE.fullmatch(url.strip())
    if not match:
        raise ValueError(f"Not a valid Telegram message range: {url}")

    link1, link2 = parse_tg_link(match.group(1)), parse_tg_link(match.group(2))
    if link1.raw_chat != link2.raw_chat:
        raise ValueError(
            f"Range URLs must reference the same channel: "
            f"'{link1.raw_chat}' != '{link2.raw_chat}'"
        )
    return (link1, link2) if link1.message_id <= link2.message_id else (link2, link1)


def _chat_title(entity) -> str:
    return getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")


def _failed(error: str, chat_title: str = "") -> dict:
    return {
        "ok": False, "error": error, "files": [], "file_metadata": [],
        "caption": "", "chat_title": chat_title,
    }


async def download_tg_message(
    client,
    url: str,
    output_dir: str = "./downloads/tg",
    progress_callback=None,
) -> dict:
    try:
        link = parse_tg_link(url)
    except ValueError as exc:
        return _failed(str(exc))

    try:
        entity = await client.get_entity(link.chat_id)
    except Exception as exc:
        return _failed(f"Cannot access chat `{link.raw_chat}`: {exc}")

    try:
        message = await client.get_messages(entity, ids=link.message_id)
    except Exception as exc:
        return _failed(f"Cannot fetch message #{link.message_id}: {exc}")

    if message is None:
        return _failed(f"Message #{link.message_id} not found (may be deleted or inaccessible).")

    chat_title = _chat_title(entity)
    caption = message.text or ""

    if message.media is None:
        return {
            "ok": False,
            "error": "Message has no media (text-only message).",
            "files": [], "file_metadata": [],
            "caption": caption, "chat_title": chat_title,
        }

    os.makedirs(output_dir, exist_ok=True)

    group_messages = [message]
    if getattr(message, "grouped_id", None):
        fetched = await client.get_messages(
            entity, ids=list(range(link.message_id - 10, link.message_id + 10))
        )
        group_messages = sorted(
            (m for m in fetched if m and m.grouped_id == message.grouped_id),
            key=lambda m: m.id,
        )

    files, metadata = [], []
    for msg in group_messages:
        if msg.media is None:
            continue
        try:
            path = await client.download_media(msg, file=output_dir, progress_callback=progress_callback)
            if path:
                files.append(path)
                metadata.append(_tg_media_meta(msg))
        except Exception as exc:
            logger.error("Failed to download TG media", msg_id=msg.id, error=str(exc))

    if not files:
        return {
            "ok": False,
            "error": "No downloadable media found in this message.",
            "files": [], "file_metadata": [],
            "caption": caption, "chat_title": chat_title,
        }

    return {
        "ok": True, "error": "", "files": files, "file_metadata": metadata,
        "caption": caption, "chat_title": chat_title,
    }


async def download_tg_range(
    client,
    url: str,
    output_dir: str = "./downloads/tg",
    progress_callback=None,
) -> dict:
    try:
        link_start, link_end = parse_tg_range(url)
    except ValueError as exc:
        return _failed(str(exc))

    try:
        entity = await client.get_entity(link_start.chat_id)
    except Exception as exc:
        return _failed(f"Cannot access chat `{link_start.raw_chat}`: {exc}")

    chat_title = _chat_title(entity)
    msg_ids = list(range(link_start.message_id, link_end.message_id + 1))

    try:
        messages = await client.get_messages(entity, ids=msg_ids)
    except Exception as exc:
        return _failed(f"Cannot fetch messages {link_start.message_id}-{link_end.message_id}: {exc}")

    valid = sorted((m for m in messages if m is not None), key=lambda m: m.id)
    if not valid:
        return {
            "ok": False,
            "error": f"No messages found in range {link_start.message_id}-{link_end.message_id}.",
            "files": [], "file_metadata": [],
            "caption": "", "chat_title": chat_title,
            "total_messages": len(msg_ids), "downloaded_messages": 0,
        }

    os.makedirs(output_dir, exist_ok=True)
    files, metadata, captions = [], [], []
    seen_groups: set = set()

    for msg in valid:
        if msg.media is None:
            continue

        if getattr(msg, "grouped_id", None):
            if msg.grouped_id in seen_groups:
                continue
            seen_groups.add(msg.grouped_id)

        text = msg.text or ""
        if text:
            captions.append(text)
        try:
            path = await client.download_media(msg, file=output_dir, progress_callback=progress_callback)
            if path:
                files.append(path)
                metadata.append(_tg_media_meta(msg))
        except Exception as exc:
            logger.error("Failed to download TG range media", msg_id=msg.id, error=str(exc))

    if not files:
        return {
            "ok": False,
            "error": f"No downloadable media found in range {link_start.message_id}-{link_end.message_id}.",
            "files": [], "file_metadata": [],
            "caption": "\n\n".join(captions), "chat_title": chat_title,
            "total_messages": len(valid), "downloaded_messages": 0,
        }

    return {
        "ok": True, "error": "", "files": files, "file_metadata": metadata,
        "caption": "\n\n".join(captions), "chat_title": chat_title,
        "total_messages": len(valid), "downloaded_messages": len(files),
    }
