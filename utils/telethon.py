"""
Telethon helpers that handle Telegram flood-wait and album chunking.

Telegram limits albums to 10 media items and rate-limits rapid edits/sends.
These wrappers catch ``FloodWaitError``, wait out the cooldown, and retry.

A global rate limiter gates all Telegram API calls to prevent flood warnings
when many workers run concurrently.
"""

from __future__ import annotations

import asyncio
import time

import structlog
from telethon.errors import FloodWaitError, RPCError

logger = structlog.get_logger(__name__)

ALBUM_LIMIT = 10

# -- Per-client rate limiter -------------------------------------------
# Telegram rate-limits rapid API calls per account.  Each client
# (bot, user session) gets its own lock + timestamp so they don't
# block each other.  0.3s floor prevents burst floods.

_MIN_INTERVAL = 0.3  # seconds between Telegram API calls per client
_rate_locks: dict[int, asyncio.Lock] = {}
_rate_lasts: dict[int, float] = {}


async def _rate_limit(client) -> None:
    """Sleep if needed so Telegram API calls per client are spaced out."""
    cid = id(client)
    if cid not in _rate_locks:
        _rate_locks[cid] = asyncio.Lock()
    async with _rate_locks[cid]:
        now = time.monotonic()
        last = _rate_lasts.get(cid, 0.0)
        wait = _MIN_INTERVAL - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _rate_lasts[cid] = time.monotonic()


async def safe_edit(message, text: str, **kwargs) -> bool:
    """Edit *message*, sleeping through flood-waits. Returns success."""
    await _rate_limit(getattr(message, "client", None))
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


async def safe_upload_file(client, *args, **kwargs):
    """upload_file — rate-limited with flood-wait retry."""
    await _rate_limit(client)
    try:
        return await client.upload_file(*args, **kwargs)
    except FloodWaitError as exc:
        logger.warning("Flood wait on upload", seconds=exc.seconds)
        await asyncio.sleep(exc.seconds)
        return await client.upload_file(*args, **kwargs)


async def safe_download_media(client, *args, **kwargs):
    """download_media — no rate-limit (downloads don't trigger floods)."""
    try:
        return await client.download_media(*args, **kwargs)
    except FloodWaitError as exc:
        logger.warning("Flood wait on download", seconds=exc.seconds)
        await asyncio.sleep(exc.seconds)
        return await client.download_media(*args, **kwargs)


async def safe_send_file(client, *args, **kwargs):
    """send_file — rate-limited with flood-wait retry."""
    await _rate_limit(client)
    try:
        return await client.send_file(*args, **kwargs)
    except FloodWaitError as exc:
        logger.warning("Flood wait on send", seconds=exc.seconds)
        await asyncio.sleep(exc.seconds)
        return await client.send_file(*args, **kwargs)


async def send_album(
    client,
    chat_id,
    media: list,
    captions: list[str],
    reply_to=None,
    **kwargs,
) -> int:
    """
    Send media as albums of at most 10, with per-file captions.

    Returns the number of items successfully sent. Falls back to individual
    sends when an album fails.
    """
    sent = 0
    for start in range(0, len(media), ALBUM_LIMIT):
        batch = media[start:start + ALBUM_LIMIT]
        batch_captions = captions[start:start + ALBUM_LIMIT]

        try:
            await safe_send_file(
                client, chat_id, batch,
                caption=batch_captions, reply_to=reply_to, **kwargs,
            )
            sent += len(batch)
            continue
        except Exception as exc:
            logger.warning("Album send failed, sending individually", error=str(exc))

        for item, caption in zip(batch, batch_captions):
            for attempt in range(2):
                try:
                    await safe_send_file(client, chat_id, item, caption=caption, reply_to=reply_to, **kwargs)
                    sent += 1
                    break
                except Exception as send_exc:
                    if attempt == 0:
                        logger.warning("Individual send failed, retrying", error=str(send_exc))
                        await asyncio.sleep(1.0)
                    else:
                        logger.warning("Individual send failed", error=str(send_exc))

    return sent