"""``/start`` — onboarding and status (spec FR-1, AC-1)."""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import catalog, config, paths, tiers


def upgrade_keyboard(url: str) -> InlineKeyboardMarkup | None:
    """The Upgrade button for a first welcome, when the operator has set a destination.

    AC-1 requires the button but never says where it points, so an unset URL omits it
    rather than sending users somewhere nobody chose (plan D29).
    """
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("✨ Upgrade", url=url)]])


@Client.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message) -> None:
    """Send the welcome.

    Two branches are not here yet, both because they need state this batch does not
    have: a new user is not yet recorded (so every ``/start`` currently takes the
    first-time path, and the "already started" notice is never sent), and the
    referral payload is not yet captured. Spec 7.1 requires that the payload never
    alters the copy, so ignoring it is the correct behaviour in the meantime.
    """
    settings = config.get_settings()
    user = message.from_user
    user_id = user.id if user else None

    tier = tiers.resolve(settings, is_admin=tiers.is_admin(settings, user_id))
    has_session = (
        user_id is not None
        and paths.session_file(user_id, test_mode=settings.test_mode).exists()
    )

    await message.reply_text(
        catalog.welcome_message(
            (user.first_name if user else None) or "there",
            tier,
            magic_word=settings.ux.default_magic_word,
            has_session=has_session,
            # No session manager exists yet, so a stored file can never be live.
            is_live=False,
        ),
        reply_markup=upgrade_keyboard(settings.ux.upgrade_url),
    )
