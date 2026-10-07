"""
Telethon helpers that handle Telegram flood-wait and album chunking.

Telegram limits albums to 10 media items and rate-limits rapid edits/sends.
These wrappers catch ``FloodWaitError``, wait out the cooldown, and retry.

Every send carries a deterministic ``random_id``, so Telegram itself drops
retries of a message that already landed — the retry paths here (album →
individual, worker re-run, duplicate handler) cannot duplicate uploads.

A global rate limiter gates all Telegram API calls to prevent flood warnings
when many workers run concurrently.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import structlog
from telethon import functions, types
from telethon.errors import FloodWaitError, RPCError

logger = structlog.get_logger(__name__)

ALBUM_LIMIT = 10

# -- Per-client rate limiter -------------------------------------------
# Telegram rate-limits rapid API calls per account.  Each client
# (bot, user session) gets its own lock + timestamp so they don't
# block each other.  0.3s floor prevents burst floods.
#
# Two lanes per client: ``media`` (uploads/sends) and ``control``
# (status edits).  They must not share a lock — a status edit sleeping
# out a flood-wait would otherwise stall the upload stream, and vice
# versa.

_MIN_INTERVAL = 0.3  # seconds between Telegram API calls per client
_rate_locks: dict[tuple[int, str], asyncio.Lock] = {}
_rate_lasts: dict[tuple[int, str], float] = {}


def _random_id(*parts) -> int:
    """Deterministic int64 for Telegram's server-side dedupe window.

    ``random_id`` is the message identity Telegram deduplicates on: a send
    that repeats an id the server already saw for that peer is silently
    dropped.  Deriving it from stable content (chat + caption) means *every*
    retry path — album→individual fallback, duplicate handler, worker
    re-run — becomes idempotent instead of duplicating uploads.
    """
    raw = hashlib.blake2b(
        "\x1f".join(str(part) for part in parts).encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(raw, "big", signed=True)


def _dedupe_ids(chat_id, captions: list[str]) -> list[int]:
    """One stable ``random_id`` per item, disambiguating repeated captions."""
    seen: dict[str, int] = {}
    ids: list[int] = []
    for caption in captions:
        occurrence = seen.get(caption, 0)
        seen[caption] = occurrence + 1
        ids.append(_random_id(chat_id, caption, occurrence))
    return ids


def _reply_to(reply_to):
    """Normalise a Message/int into the ``InputReplyTo`` the raw API wants."""
    if reply_to is None:
        return None
    msg_id = getattr(reply_to, "id", reply_to)
    if not isinstance(msg_id, int) or msg_id <= 0:
        return None
    return types.InputReplyToMessage(reply_to_msg_id=msg_id)


async def _rate_limit(client, kind: str = "media") -> None:
    """Sleep if needed so Telegram API calls per client are spaced out.

    Only user clients are throttled (tagged ``_spidey_user`` at handler
    registration).  The shared bot client passes through instantly.
    *kind* selects the lane: ``media`` or ``control``.
    """
    if not getattr(client, "_spidey_user", False):
        return
    key = (id(client), kind)
    lock = _rate_locks.get(key)
    if lock is None:
        lock = _rate_locks[key] = asyncio.Lock()
    async with lock:
        now = time.monotonic()
        last = _rate_lasts.get(key, 0.0)
        wait = _MIN_INTERVAL - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _rate_lasts[key] = time.monotonic()


async def _raw_call(client, build_request):
    """Await ``client(build_request())``, sleeping through one flood-wait.

    *build_request* is a factory because Telethon requests are single-use.
    """
    await _rate_limit(client)
    try:
        return await client(build_request())
    except FloodWaitError as exc:
        logger.warning("Flood wait on raw call", seconds=exc.seconds)
        await asyncio.sleep(exc.seconds)
        await _rate_limit(client)
        return await client(build_request())


async def safe_edit(message, text: str, **kwargs) -> bool:
    """Edit *message*, sleeping through flood-waits. Returns success."""
    await _rate_limit(getattr(message, "client", None), "control")
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


async def safe_send_file(client, entity, file, caption=None, **kwargs):
    """send_file — rate-limited, flood-safe, and duplicate-safe.

    A list of media is routed through :func:`send_album` so it inherits the
    deterministic ``random_id`` dedupe; anything else goes straight to
    Telethon.
    """
    if isinstance(file, (list, tuple)):
        media = list(file)
        if not media:
            return None
        if isinstance(caption, (list, tuple)):
            captions = list(caption)
        else:
            captions = [caption or ""] + [""] * (len(media) - 1)
        await send_album(client, entity, media, captions, **kwargs)
        return None
    await _rate_limit(client)
    try:
        return await client.send_file(entity, file, caption=caption, **kwargs)
    except FloodWaitError as exc:
        logger.warning("Flood wait on send", seconds=exc.seconds)
        await asyncio.sleep(exc.seconds)
        return await client.send_file(entity, file, caption=caption, **kwargs)


async def send_album(
    client,
    chat_id,
    media: list,
    captions: list[str],
    reply_to=None,
    **kwargs,
) -> int:
    """Send media as albums of at most 10, with per-file captions.

    Returns the number of items successfully sent.

    Every item carries a ``random_id`` derived from the chat and its caption,
    so Telegram drops any repeat of a message that already landed.  When an
    album is rejected (mixed photo/video, unsupported group, …) the same
    items are re-sent individually **with the same ids** — a partial album
    therefore cannot duplicate, it can only be completed.
    """
    if not media:
        return 0

    try:
        peer = await client.get_input_entity(chat_id)
    except Exception:
        peer = chat_id

    opts = {
        "silent": kwargs.get("silent"),
        "noforwards": kwargs.get("noforwards"),
        "schedule_date": kwargs.get("schedule_date"),
    }

    sent = 0
    for start in range(0, len(media), ALBUM_LIMIT):
        batch = media[start:start + ALBUM_LIMIT]
        batch_captions = captions[start:start + ALBUM_LIMIT]
        if len(batch_captions) < len(batch):
            batch_captions += [""] * (len(batch) - len(batch_captions))
        ids = _dedupe_ids(chat_id, batch_captions)

        if await _send_album_batch(client, peer, batch, batch_captions, ids, reply_to, opts):
            sent += len(batch)
            continue

        logger.warning("Album send failed, sending individually", count=len(batch))
        sent += await _send_items(client, peer, batch, batch_captions, ids, reply_to, opts)

    return sent


async def _send_album_batch(client, peer, batch, captions, ids, reply_to, opts) -> bool:
    """One ``SendMultiMedia`` call. Returns whether it was accepted."""
    def build():
        return functions.messages.SendMultiMediaRequest(
            peer=peer,
            multi_media=[
                types.InputSingleMedia(media=item, random_id=rid, message=cap or "")
                for item, cap, rid in zip(batch, captions, ids)
            ],
            reply_to=_reply_to(reply_to),
            silent=opts.get("silent"),
            noforwards=opts.get("noforwards"),
            schedule_date=opts.get("schedule_date"),
        )

    try:
        await _raw_call(client, build)
        return True
    except Exception as exc:
        logger.warning("Album send failed", error=str(exc))
        return False


async def _send_items(client, peer, batch, captions, ids, reply_to, opts) -> int:
    """Send each item alone, reusing the album's ``random_id`` per item."""
    sent = 0
    for item, cap, rid in zip(batch, captions, ids):
        def build(item=item, cap=cap, rid=rid):
            return functions.messages.SendMediaRequest(
                peer=peer,
                media=item,
                message=cap or "",
                random_id=rid,
                reply_to=_reply_to(reply_to),
                silent=opts.get("silent"),
                noforwards=opts.get("noforwards"),
                schedule_date=opts.get("schedule_date"),
            )

        for attempt in range(2):
            try:
                await _raw_call(client, build)
                sent += 1
                break
            except Exception as exc:
                if attempt == 0:
                    logger.warning("Individual send failed, retrying", error=str(exc))
                    await asyncio.sleep(1.0)
                else:
                    logger.warning("Individual send failed", error=str(exc))
    return sent