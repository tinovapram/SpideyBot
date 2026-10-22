"""
Telethon helpers that handle Telegram flood-wait and album chunking.

Every send and edit is paced by the client's :class:`~core.sendgate.SendGate`
(shared flood cooldown, per-chat/per-client token buckets, bounded retries).
Telegram limits albums to 10 media items and only accepts photos and videos
inside a media group, so anything else is delivered as its own message.
"""

from __future__ import annotations

import structlog
from telethon import types
from telethon.errors import RPCError

from core import sendgate
from core.sendgate import FileExpiredError

logger = structlog.get_logger(__name__)

ALBUM_LIMIT = 10
# Telegram's own hard limit is 10, but ten large videos make its workers answer
# with WorkerBusyTooLongRetryError, so video albums are grouped more tightly.
VIDEO_ALBUM_LIMIT = 5


def is_album_media(item: object) -> bool:
    """Return True when *item* may be grouped into a Telegram album.

    Albums carry photos and videos only; documents, audio, archives and
    animations have to be sent individually.
    """
    if isinstance(item, types.InputMediaUploadedPhoto):
        return True
    return _is_video_media(item)


def _is_video_media(item: object) -> bool:
    """Return True for an uploaded video (animations excluded)."""
    if not isinstance(item, types.InputMediaUploadedDocument):
        return False
    attributes = item.attributes or []
    if any(isinstance(attr, types.DocumentAttributeAnimated) for attr in attributes):
        return False
    return any(isinstance(attr, types.DocumentAttributeVideo) for attr in attributes)


def album_chunks(
    batch: list,
    *,
    limit: int = ALBUM_LIMIT,
    video_limit: int = VIDEO_ALBUM_LIMIT,
) -> list[tuple[int, int]]:
    """Split *batch* into ``(start, end)`` windows of album-sized sends.

    A window containing a video is capped at *video_limit*; photos alone may use
    the full Telegram limit.
    """
    windows: list[tuple[int, int]] = []
    start = 0
    total = len(batch)
    while start < total:
        window = batch[start:start + limit]
        size = video_limit if any(_is_video_media(item) for item in window) else limit
        end = min(start + max(1, size), total)
        windows.append((start, end))
        start = end
    return windows


async def safe_edit(message, text: str, **kwargs) -> bool:
    """Edit *message* under the client's cooldown (no send tokens). Returns success."""
    gate = sendgate.get_gate(getattr(message, "_client", None))
    try:
        await gate.edit(lambda: message.edit(text, **kwargs))
        return True
    except RPCError:
        return False


async def safe_send_file(client, chat_id, media, **kwargs):
    """``send_file`` paced by *client*'s gate, with retries for retryable errors."""
    gate = sendgate.get_gate(client)
    return await gate.send(chat_id, lambda: client.send_file(chat_id, media, **kwargs))


async def _send_individual(
    client,
    chat_id,
    item,
    caption: str,
    reply_to,
    **kwargs,
) -> bool:
    """Send one media item; the gate already handles retries."""
    try:
        await safe_send_file(
            client, chat_id, item,
            caption=caption, reply_to=reply_to, **kwargs,
        )
        return True
    except Exception as exc:
        logger.warning("Individual send failed", error=str(exc))
        return False


async def _send_album_batch(
    client,
    chat_id,
    batch: list,
    captions: list[str],
    reply_to,
    **kwargs,
) -> int:
    """Send one album (≤ ``ALBUM_LIMIT``), falling back to individual sends."""
    try:
        await safe_send_file(
            client, chat_id, batch,
            caption=captions, reply_to=reply_to, **kwargs,
        )
        return len(batch)
    except FileExpiredError:
        logger.warning("Album media expired, batch dropped", count=len(batch))
        return 0
    except Exception as exc:
        logger.warning("Album send failed, sending individually", error=str(exc))

    sent = 0
    for offset, item in enumerate(batch):
        caption = captions[offset] if offset < len(captions) else ""
        if await _send_individual(client, chat_id, item, caption, reply_to, **kwargs):
            sent += 1
    return sent


async def send_album(
    client,
    chat_id,
    media: list,
    captions: list[str],
    reply_to=None,
    **kwargs,
) -> int:
    """Send photos/videos as albums (≤10) and every other file individually.

    Returns the number of items successfully sent. Order is preserved: an
    album is flushed before a document/audio item is sent on its own.
    """
    sent = 0
    pending: list = []
    pending_captions: list[str] = []

    async def flush() -> None:
        nonlocal sent
        if not pending:
            return
        batch = pending[:]
        batch_captions = pending_captions[:]
        pending.clear()
        pending_captions.clear()
        for start, end in album_chunks(batch):
            sent += await _send_album_batch(
                client,
                chat_id,
                batch[start:end],
                batch_captions[start:end],
                reply_to,
                **kwargs,
            )

    for index, item in enumerate(media):
        caption = captions[index] if index < len(captions) else ""
        if is_album_media(item):
            pending.append(item)
            pending_captions.append(caption)
            if len(pending) >= ALBUM_LIMIT:
                await flush()
            continue

        await flush()
        if await _send_individual(client, chat_id, item, caption, reply_to, **kwargs):
            sent += 1

    await flush()
    return sent
