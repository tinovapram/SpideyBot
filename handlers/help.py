"""``/help`` — the command guide (spec FR-2, copy in spec 7.1)."""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog, config, tiers


@Client.on_message(filters.command("help") & filters.private)
async def help_command(client: Client, message: Message) -> None:
    settings = config.get_settings()
    user_id = message.from_user.id if message.from_user else None

    await message.reply_text(
        catalog.help_message(
            magic_word=settings.ux.default_magic_word,
            is_admin=tiers.is_admin(settings, user_id),
        )
    )
