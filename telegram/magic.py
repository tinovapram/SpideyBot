"""The reply magic word (FR-5, AC-8).

The user replies to a media message anywhere, types their word, and the media lands in their Saved
Messages. Two consequences shape the implementation:

* **This runs on the user's own client.** The message being reacted to is *the user's*, and Telegram
  only tells an account about its own outgoing messages — which is why the pool registers this
  handler when it connects a session, and why FR-5.3 insists only the user's own outgoing messages
  count. A handler on the bot client would see other people's messages and none of the user's.
* **The word is matched, not parsed.** FR-5.2 makes it case-insensitive and single-token:
  ``WOW`` and ``wow`` are the same word, and a sentence containing it is not the word. Anything
  else in the chat is left alone, silently — a bot that commented on every message would be
  unusable.

Everything about the outcome is one message to Saved Messages, because that is the only chat the bot
may speak in: posting into a third-party chat as the user is not this feature's business.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable
from typing import Any

from pyrogram import filters
from pyrogram import handlers as tg_handlers

from app import catalog, paths, prefs
from app.config import Settings
from app.db import session as db_session
from telegram import media

log = logging.getLogger(__name__)

#: Handlers for one user client are grouped so they can be removed together, and so a later feature
#: (P7's settings menu) can add its own without needing to know this one's group.
MAGIC_GROUP = 20


def is_magic(text: str | None, word: str) -> bool:
    """Whether this text *is* the word: one token, any case, nothing else (FR-5.2, FR-5.3)."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped or any(character.isspace() for character in stripped):
        return False
    return stripped.casefold() == word.strip().casefold()


async def handle(
    client: Any,
    message: Any,
    *,
    settings: Settings,
    user_id: int,
) -> str | None:
    """React to one message; return the sentence that was stored, or ``None`` for "not the word".

    Returning the copy rather than only sending it keeps the wording testable without a client, and
    the caller (a filter-registered handler) does not re-derive it.
    """
    async with db_session() as db:
        word = await prefs.magic_word(db, settings, user_id)

    # FR-5.3 is enforced here as well as in the handler's filter, on purpose: the filter only runs
    # for messages the *dispatcher* routes, so a rule living only there would be untestable and
    # would vanish the moment this function is called from anywhere else. One flag compared twice is
    # cheap; acting on a stranger's message is not.
    if not getattr(message, "outgoing", False):
        return None

    if not is_magic(getattr(message, "text", None), word):
        return None

    target = getattr(message, "reply_to_message", None)
    if target is None:
        await client.send_message(media.SELF, catalog.MAGIC_NO_REPLY.format(word=word))
        return catalog.MAGIC_NO_REPLY.format(word=word)

    if getattr(target, "media", None) is None:
        await client.send_message(media.SELF, catalog.MAGIC_NO_MEDIA.format(word=word))
        return catalog.MAGIC_NO_MEDIA.format(word=word)

    scratch = paths.TMP_DIR / f"magic-{user_id}"
    try:
        await media.send_to_self(
            client, target, caption=catalog.MAGIC_SAVED, scratch=scratch
        )
    except Exception as failed:  # noqa: BLE001 - the user is told, and the client stays alive
        log.warning("magic word: saving media for %s failed: %s", user_id, failed)
        await _complain(client, catalog.MAGIC_FAILED.format(reason=type(failed).__name__))
        return catalog.MAGIC_FAILED.format(reason=type(failed).__name__)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return catalog.MAGIC_SAVED


async def _complain(client: Any, text: str) -> None:
    """Tell the user, tolerating a client that cannot (it may already be gone)."""
    with contextlib.suppress(Exception):
        await client.send_message(media.SELF, text)


def register(
    client: Any,
    settings: Settings,
    *,
    user_id: int,
    on_error: Callable[[Exception], Awaitable[None] | None] | None = None,
) -> None:
    """Attach the magic word to a live user client (FR-5).

    The filter is deliberately as wide as ``outgoing text``: the word is per-user and can be changed
    at any time (FR-14, `P7`), so a filter that matched the *word* would have to be replaced on
    every change — and a handler that is replaced is one that can be forgotten. One comparison per
    message is cheaper than that bookkeeping.
    """

    async def on_message(client: Any, message: Any) -> None:
        try:
            await handle(client, message, settings=settings, user_id=user_id)
        except Exception as failed:  # noqa: BLE001 - a user client must not die on one message
            log.exception("magic word handler failed")
            if on_error is not None:
                result = on_error(failed)
                if isinstance(result, Awaitable):
                    await result

    client.add_handler(
        tg_handlers.MessageHandler(on_message, filters.outgoing & filters.text),
        group=MAGIC_GROUP,
    )
