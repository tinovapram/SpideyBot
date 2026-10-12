"""Per-user preferences: the magic word and auto-save (FR-5.2, FR-6.4, FR-14).

Two settings, and two rules that matter more than the storage:

* **Unset is not the same as off.** ``user_prefs.timer_media`` is nullable on purpose (the model
  says so): ``NULL`` means the user never touched it, so the default applies. Storing the default as
  a value would freeze today's default into every row, and a later change to the configuration
  would then apply only to people who had not answered — the opposite of what a default is for.
* **Reading a preference fails open** (FR-6.4). If the row cannot be read — a database blink, a
  broken migration — auto-save stays **on**. The failure that matters here is silent loss of media
  the user expected to keep, and "we could not check, so we did nothing" is the one outcome the
  spec rules out explicitly.

The default magic word comes from configuration (``ux.default_magic_word``, D29) rather than being
written here, so the copy the user reads in ``/help`` and the word the handler matches cannot drift.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app import users
from app.config import Settings
from app.models import UserPref

log = logging.getLogger(__name__)


async def magic_word(db: AsyncSession, settings: Settings, user_id: int) -> str:
    """The word this user types to save a replied message (FR-5.2).

    A stored word wins; otherwise the configured default. An empty stored value is treated as unset,
    because ``/setting`` clearing the field is a request to go back to the default, not to match the
    empty string.
    """
    default = settings.ux.default_magic_word
    try:
        pref = await db.get(UserPref, user_id)
    except Exception as failed:  # noqa: BLE001 - a preference must never break a feature
        log.warning("could not read the magic word for %s: %s", user_id, failed)
        return default
    if pref is None or not (pref.magic_word or "").strip():
        return default
    return pref.magic_word.strip()


async def timer_media(db: AsyncSession, settings: Settings, user_id: int) -> bool:
    """Whether incoming private media is auto-saved. Unset and unreadable both mean on (FR-6.4)."""
    try:
        pref = await db.get(UserPref, user_id)
    except Exception as failed:  # noqa: BLE001 - fail open, by requirement
        log.warning("could not read the TimerMedia preference for %s: %s", user_id, failed)
        return True
    if pref is None or pref.timer_media is None:
        return True
    return bool(pref.timer_media)


async def set_magic_word(db: AsyncSession, user_id: int, word: str | None) -> None:
    """Store a magic word, or clear it back to the default with ``None``."""
    await _upsert(db, user_id, magic_word=word)


async def set_timer_media(db: AsyncSession, user_id: int, enabled: bool) -> None:
    await _upsert(db, user_id, timer_media=enabled)


async def _upsert(db: AsyncSession, user_id: int, **values: object) -> None:
    """Write one preference, creating the row if this user has none yet.

    Read-modify-write rather than an upsert statement: the row is one per user and the caller is a
    single chat command, so the race it would protect against does not exist, and a merge keeps the
    other columns (a user changing their magic word must not silently switch auto-save back on).
    """
    # The row has a foreign key to ``users``, so a user the bot has never registered would fail to
    # insert here rather than at a more obvious place.
    await users.ensure_registered(db, user_id=user_id)
    pref = await db.get(UserPref, user_id)
    if pref is None:
        pref = UserPref(user_id=user_id)
        db.add(pref)
    for name, value in values.items():
        setattr(pref, name, value)
