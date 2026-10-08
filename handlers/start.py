"""``/start`` — registration, referral capture, and onboarding (FR-1, AC-1).

The order inside the database block matters: the user is registered, the referral recorded
and the onboarding flag read *before* the message is composed, because whether onboarding
has already happened is what selects the copy. Referral capture is silent by requirement
(FR-1.2) — it must not change either message.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import catalog, config, paths, referral, users
from app.db import session as db_session


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
        has_session = paths.session_file(sender.id, test_mode=settings.test_mode).exists()

    if first_visit:
        await message.reply_text(
            catalog.welcome_message(
                sender.first_name or "there",
                tier,
                magic_word=settings.ux.default_magic_word,
                has_session=has_session,
                # There is no session manager yet, so a stored file can never be live.
                is_live=False,
            ),
            reply_markup=upgrade_keyboard(settings.ux.upgrade_url),
        )
        return

    # AC-1: an onboarded user gets the short notice rather than the welcome. No Upgrade
    # button, because the notice is meant to stay brief.
    await message.reply_text(
        catalog.already_started_message(has_session=has_session, is_live=False)
    )
