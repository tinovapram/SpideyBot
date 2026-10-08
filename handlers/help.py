"""``/help`` — the command guide (spec FR-2, copy in spec 7.1).

FR-2.4 requires the output to reflect the *caller's* limits rather than global defaults, so
the tier is resolved for the sender rather than read off the config.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog, config, paths, tiers, users
from app.db import session as db_session


@Client.on_message(filters.command("help") & filters.private)
async def help_command(client: Client, message: Message) -> None:
    settings = config.get_settings()
    sender = message.from_user
    user_id = sender.id if sender else None

    async with db_session() as db:
        record = await users.fetch(db, user_id) if user_id is not None else None
        tier = (
            users.tier_for(settings, record)
            if record is not None
            # Someone who has never run /start still deserves a correct answer, so fall
            # back to the defaults that would apply to them.
            else tiers.resolve(settings, is_admin=tiers.is_admin(settings, user_id))
        )
        has_session = (
            user_id is not None
            and paths.session_file(user_id, test_mode=settings.test_mode).exists()
        )

    header = catalog.tier_block(tier, has_session=has_session, is_live=False)
    body = catalog.help_message(
        magic_word=settings.ux.default_magic_word,
        is_admin=tier.is_admin,
    )
    await message.reply_text(f"{header}\n\n{body}")
