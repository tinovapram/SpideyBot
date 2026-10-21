"""
Telethon helpers that handle Telegram flood-wait and album chunking.

Telegram limits albums to 10 media items, rate-limits rapid edits/sends, and
only accepts photos and videos inside a media group. These wrappers catch
``FloodWaitError``, wait out the cooldown, retry, and keep every other file
type out of the album so it is delivered as its own message.
"""

from __future__ import annotations

import asyncio

import structlog
from telethon import types
from telethon.errors import FloodWaitError, RPCError

logger = structlog.get_logger(__name__)

ALBUM_LIMIT = 10


def is_album_media(item: object) -> bool:
    """Return True when *item* may be grouped into a Telegram album.

    Albums carry photos and videos only; documents, audio, archives and
    animations have to be sent individually.
    """
    if isinstance(item, types.InputMediaUploadedPhoto):
        return True
    if isinstance(item, types.InputMediaUploadedDocument):
        attributes = item.attributes or []
        if any(isinstance(attr, types.DocumentAttributeAnimated) for attr in attributes):
            return False
        return any(isinstance(attr, types.DocumentAttributeVideo) for attr in attributes)
    return False


async def safe_edit(message, text: str, **kwargs) -> bool:
    """Edit *message*, sleeping through flood-waits. Returns success."""
    try:
        await message.edit(text, **kwargs)
        return True
    except FloodWaitError as exc:
        logger.warning("Flood wait on edit", seconds=exc.seconds)
        await asyncio.sleep(exc.seconds)
        try:
            await message.edit(text, **kwargs)
            return True
        except RPCError:
            return False
    except RPCError:
        return False


async def safe_send_file(client, *args, **kwargs):
    """``send_file`` that sleeps through a single flood-wait and retries."""
    try:
        return await client.send_file(*args, **kwargs)
    except FloodWaitError as exc:
        logger.warning("Flood wait on send", seconds=exc.seconds)
        await asyncio.sleep(exc.seconds)
        return await client.send_file(*args, **kwargs)


async def _send_individual(
    client,
    chat_id,
    item,
    caption: str,
    reply_to,
    **kwargs,
) -> bool:
    """Send one media item, retrying once after a short pause."""
    for attempt in range(2):
        try:
            await safe_send_file(
                client, chat_id, item,
                caption=caption, reply_to=reply_to, **kwargs,
            )
            return True
        except Exception as send_exc:
            if attempt == 0:
                logger.warning("Individual send failed, retrying", error=str(send_exc))
                await asyncio.sleep(1.0)
            else:
                logger.warning("Individual send failed", error=str(send_exc))
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
        for start in range(0, len(batch), ALBUM_LIMIT):
            sent += await _send_album_batch(
                client,
                chat_id,
                batch[start:start + ALBUM_LIMIT],
                batch_captions[start:start + ALBUM_LIMIT],
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
