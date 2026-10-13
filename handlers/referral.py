"""``/referral`` — check referral stats (FR-17)."""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message
from sqlalchemy import func, select

from app.db import session as db_session
from app.models import Referral


@Client.on_message(filters.command("referral") & filters.private)
async def referral_command(client: Client, message: Message) -> None:
    sender = message.from_user
    if sender is None:
        return

    # FR-17.1: link generation requires a username or bot instance
    bot_info = await client.get_me()
    if not bot_info.username:
        await message.reply_text("⚠️ Cannot build referral link: bot has no public username.")
        return

    invite_link = f"https://t.me/{bot_info.username}?start={sender.id}"

    async with db_session() as db:
        # FR-17.3: Invited (total), Credited, Pending.
        referrals = await db.execute(
            select(Referral.status, func.count())
            .where(Referral.referrer_id == sender.id)
            .group_by(Referral.status)
        )
        counts = {status: count for status, count in referrals.all()}
        invited = sum(counts.values())
        pending = counts.get("pending", 0)
        credited = counts.get("credited", 0)

    text = (
        "👥 **Referral Program**\n\n"
        f"Your invite link: `{invite_link}`\n\n"
        f"Invited: {invited}\n"
        f"Pending: {pending}\n"
        f"Credited: {credited}\n\n"
        "Credit rule: referral is credited on invitee's first successful download.\n"
        "Reward: +10 downloads/day for 30 days."
    )
    await message.reply_text(text)
