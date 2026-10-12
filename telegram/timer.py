"""TimerMedia: auto-save incoming private media (FR-6, AC-9).

On by default, and on when nothing can be read (FR-6.4) — the spec's fail-open rule is explicit, and
the failure it prevents is losing a self-destructing photo the user expected the bot to keep.

**``media_unread`` is not a field Kurigram exposes.** The spec names it because Telethon has it;
Pyrogram does not, so the rule is implemented as what it *means*: media that arrives while the
user's client is live and that we have not already saved. Two things make that honest:

* Kurigram's ``skip_updates`` default is on, so the backlog that accumulated while the client was
  offline is never dispatched at all. "Read" media from before this session is therefore invisible,
  not merely filtered — which is exactly the difference the spec cares about.
* a small per-process set of ``(chat, message)`` pairs stops a second delivery if the same update
  is seen twice, which a reconnect can cause.

The known gap, recorded rather than hidden (D55): a private photo that arrives *while the client is
connected* and is then read on another device will still be saved, because nothing in Kurigram
reports the read transition.
"""

from __future__ import annotations

import logging
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from pyrogram import filters
from pyrogram import handlers as tg_handlers

from app import catalog, paths, prefs
from app.config import Settings
from app.db import session as db_session
from telegram import media

log = logging.getLogger(__name__)

#: One group per user client, separate from the magic word's, so each can be removed on its own.
TIMER_GROUP = 21

#: How many recently saved messages one process remembers. Small on purpose: this is a debounce for
#: a repeated update, not a history — a large set here would only hide a bug elsewhere.
SEEN_LIMIT = 512

_SEEN: deque[tuple[int, int]] = deque(maxlen=SEEN_LIMIT)


def _key(message: Any) -> tuple[int, int]:
    chat = getattr(message, "chat", None)
    chat_id = int(getattr(chat, "id", 0) or 0)
    return chat_id, int(getattr(message, "id", 0) or 0)


def is_candidate(message: Any) -> bool:
    """FR-6.2's scope: incoming, private, photo or video, and not our own."""
    if getattr(message, "outgoing", False):
        return False
    chat = getattr(message, "chat", None)
    kind = str(getattr(getattr(chat, "type", None), "name", "")).lower()
    if kind and kind != "private":
        return False
    return bool(getattr(message, "photo", None) or getattr(message, "video", None))


def sender_caption(message: Any) -> str:
    """FR-6.3's caption: who sent it, which account, and what they wrote."""
    sender = getattr(message, "from_user", None)
    username = getattr(sender, "username", None)
    sender_id = getattr(sender, "id", None)
    text = getattr(message, "caption", None) or getattr(message, "text", None) or ""
    return catalog.TIMER_CAPTION.format(
        username=f"@{username}" if username else "no username",
        sender_id=sender_id if sender_id is not None else "unknown",
        text=text.strip() or "no caption",
    )


async def handle(
    client: Any,
    message: Any,
    *,
    settings: Settings,
    user_id: int,
) -> str | None:
    """Save one incoming message if it is in scope and the toggle allows it. Returns the sentence.

    ``None`` means "not ours": not private media, or the user turned it off, or it was already
    saved.
    """
    if not is_candidate(message):
        return None
    key = _key(message)
    if key in _SEEN:
        return None

    async with db_session() as db:
        enabled = await prefs.timer_media(db, settings, user_id)
    if not enabled:
        return None

    scratch = paths.TMP_DIR / f"timer-{user_id}"
    try:
        await media.send_to_self(
            client,
            message,
            caption=sender_caption(message),
            scratch=scratch,
        )
    except Exception as failed:  # noqa: BLE001 - one unsaveable photo must not stop the rest
        log.warning("TimerMedia: saving %s failed: %s", key, failed)
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    _SEEN.append(key)
    log.info("TimerMedia saved %s for %s", key, user_id)
    return catalog.TIMER_SAVED


def register(
    client: Any,
    settings: Settings,
    *,
    user_id: int,
    on_error: Callable[[Exception], Awaitable[None] | None] | None = None,
) -> None:
    """Attach auto-save to a live user client (FR-6).

    The filter is ``private & (photo | video)``; everything else FR-6.2 asks for is decided in
    :func:`is_candidate`, because a filter that silently dropped a message would make the reason
    untestable.
    """

    async def on_message(client: Any, message: Any) -> None:
        try:
            await handle(client, message, settings=settings, user_id=user_id)
        except Exception as failed:  # noqa: BLE001 - fail open, always
            log.exception("TimerMedia handler failed")
            if on_error is not None:
                result = on_error(failed)
                if isinstance(result, Awaitable):
                    await result

    client.add_handler(
        tg_handlers.MessageHandler(
            on_message, filters.private & (filters.photo | filters.video)
        ),
        group=TIMER_GROUP,
    )


def forget() -> None:
    """Forget what has been saved. Only a test needs this; the process owns the set."""
    _SEEN.clear()
