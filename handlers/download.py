"""``/dl`` — generic link download with quota validation and queue entry (FR-3, AC-2, AC-3, AC-10).

The handler validates the request against the user's quota *before* enqueueing, then posts the
initial status message and returns. The worker pool picks up the job from there.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog, config, quota, users
from app.db import session as db_session
from app.queue import counts as job_counts
from app.queue import enqueue
from app.tiers import get_limits


@Client.on_message(filters.command("dl") & filters.private)
async def dl_command(client: Client, message: Message) -> None:
    settings = config.get_settings()
    sender = message.from_user
    if sender is None:
        return

    # Extract URL from command
    command = getattr(message, "command", None) or []
    if len(command) < 2:
        await message.reply_text(catalog.USAGE_DL)
        return

    url = command[1].strip()
    if not url:
        await message.reply_text(catalog.USAGE_DL)
        return

    async with db_session() as db:
        user = await users.ensure_registered(
            db,
            user_id=sender.id,
            username=sender.username,
            first_name=sender.first_name,
        )
        tier = users.tier_for(settings, user)
        limits = get_limits(settings, tier.name)

        # Check daily download allowance
        usage = await quota.current(db, sender.id)
        credits = await quota.active_bonus_credits(db, sender.id)
        allowance = quota.download_allowance(
            settings, tier_daily_downloads=limits.daily_downloads, credits=credits
        )

        if not await quota.reserve_download(db, sender.id, limit=allowance, bonus=credits):
            await message.reply_text(catalog.quota_message(
                tier, usage, bonus=quota.bonus_allowance(settings, credits),
                slots_active=0, queued=0
            ))
            return

        # Check concurrent slots
        counts = await job_counts(db, sender.id)
        if counts.running >= limits.concurrent:
            # Release the reservation since we can't run now
            await quota.release_download(db, sender.id)
            await message.reply_text(catalog.quota_message(
                tier, usage, bonus=quota.bonus_allowance(settings, credits),
                slots_active=counts.running, queued=counts.queued + 1
            ))
            return

        # Check bandwidth
        if quota.bandwidth_exhausted(usage, limits.daily_mb):
            await quota.release_download(db, sender.id)
            await message.reply_text(catalog.quota_message(
                tier, usage, bonus=quota.bonus_allowance(settings, credits),
                slots_active=counts.running, queued=counts.queued
            ))
            return

        # All checks passed - enqueue the job
        job = await enqueue(
            db,
            user_id=sender.id,
            link=url,
            tier=tier.name,
            priority=limits.priority,
            site=None,  # Will be determined by the pipeline
        )
        await db.commit()

    # Post initial status message
    status_msg = await message.reply_text(
        catalog.QUEUING_DOWNLOAD.format(
            id=job.id,
            link=url,
            position=counts.queued + 1,
        )
    )

    # Update job with status message info for live updates
    async with db_session() as db:
        job.status_chat_id = status_msg.chat.id
        job.status_message_id = status_msg.id
        await db.commit()