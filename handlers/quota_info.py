"""``/quota`` — today's usage against this user's limits (FR-15, AC-10).

Read-only by design: refusing a submission belongs to the download path, and this command only
has to *explain* what that path will decide. It therefore reads the same numbers the gates use
— the tier resolution, the usage row, the referral credits and the live job counts — rather
than recomputing anything, so the answer cannot drift away from the enforcement.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog, config, quota, users
from app.db import session as db_session
from app.queue import counts as job_counts


@Client.on_message(filters.command("quota") & filters.private)
async def quota_command(client: Client, message: Message) -> None:
    settings = config.get_settings()
    sender = message.from_user
    if sender is None:
        # A channel post has no quota to report.
        return

    async with db_session() as db:
        user = await users.ensure_registered(
            db,
            user_id=sender.id,
            username=sender.username,
            first_name=sender.first_name,
        )
        tier = users.tier_for(settings, user)
        usage = await quota.current(db, sender.id)
        credits = await quota.active_bonus_credits(db, sender.id)
        counts = await job_counts(db, sender.id)

    await message.reply_text(
        catalog.quota_message(
            tier,
            usage,
            bonus=quota.bonus_allowance(settings, credits),
            slots_active=counts.running,
            queued=counts.queued,
        )
    )
