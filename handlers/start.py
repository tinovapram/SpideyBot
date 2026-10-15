"""``/start`` — registration, referral capture, and onboarding (FR-1, AC-1).

The order inside the database block matters: the user is registered, the referral recorded
and the onboarding flag read *before* the message is composed, because whether onboarding
has already happened is what selects the copy. Referral capture is silent by requirement
(FR-1.2) — it must not change either message.
"""

from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import catalog, config, referral, users
from app.config import Settings
from app.db import session as db_session
from telegram import sessions
from telegram.sessions import SessionError

log = logging.getLogger(__name__)


def upgrade_keyboard(url: str) -> InlineKeyboardMarkup | None:
    """The Upgrade button for a first welcome, when the operator has set a destination.

    AC-1 requires the button but never says where it points, so an unset URL omits it
    rather than sending users somewhere nobody chose (plan D29).
    """
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("✨ Upgrade", url=url)]])


def referral_payload(message: Message) -> str | None:
    """The ``/start`` argument, if one was given.

    Kurigram splits the command already, so an invite deep link arrives as
    ``["start", "<referrer_id>"]``.
    """
    command = getattr(message, "command", None) or []
    return command[1] if len(command) > 1 else None


@Client.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message) -> None:
    settings = config.get_settings()
    sender = message.from_user
    if sender is None:
        # A channel post or a service message has nobody to onboard.
        return

    async with db_session() as db:
        user = await users.upsert(
            db,
            user_id=sender.id,
            username=sender.username,
            first_name=sender.first_name,
        )

        referrer_id = referral.parse_referrer_id(referral_payload(message))
        if referrer_id is not None:
            await referral.record_pending(db, referrer_id=referrer_id, invitee_id=sender.id)

        tier = users.tier_for(settings, user)
        first_visit = await users.mark_onboarded(db, user)

    # §8.3's middle arrow: a stored session is reconnected on /start, so the badge a user reads here
    # is the real one. Reconnecting is best-effort — a session Telegram has revoked must not stop
    # /start from answering, it must just be reported as gone (the pool deletes it in that case).
    has_session, is_live = await _session_state(sender.id, settings)

    welcome = catalog.welcome_message(
        sender.first_name or "there",
        tier,
        magic_word=settings.ux.default_magic_word,
        has_session=has_session,
        is_live=is_live,
    )

    parts = []
    if first_visit:
        parts.append("Welcome new user")
    else:
        parts.append("Welcome back")
        if has_session:
            parts.append("you loged in")
            parts.append("already start your user session")
        else:
            parts.append("/login to login to your telegram account")

    parts.append(welcome)

    if not has_session:
        parts.append("'/login'")

    text = "\n\n".join(parts)

    await message.reply_text(
        text,
        reply_markup=upgrade_keyboard(settings.ux.upgrade_url) if first_visit else None,
    )


async def _session_state(user_id: int, settings: Settings) -> tuple[bool, bool]:
    """``(has_session, is_live)`` for the badge, reconnecting a stored session on the way.

    FR-1.5 asks the onboarding copy to *say* whether the session is live, which is only answerable
    by trying: a file on disk says nothing about whether Telegram still accepts the key. So a stored
    session is connected here, and the two failures that matter are both ordinary outcomes — a
    session that cannot be reached (reported as saved-but-not-running) and one Telegram has revoked
    (reported as absent, because the pool has already deleted the file).
    """
    pool = sessions.get_pool(settings)
    has_session = pool.has_session(user_id)
    if not has_session:
        return False, False

    try:
        await pool.get(user_id)
    except SessionError as refused:
        log.info("a stored session did not come back on /start: %s", refused.reason)
        return refused.expired is False and pool.has_session(user_id), False
    return True, True
