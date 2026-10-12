"""Admission: may this user start this download now, and if so, queue it (FR-3, AC-10).

``/dl`` and ``/dt`` differ only in which link they accept, so the part that decides *whether the
download may happen* lives here once. That matters more than the duplication it removes: the quota
policy is the thing AC-10 tests, and a second copy of it — for Telegram links — would be a copy that
could drift out of agreement with the first while both still looked right.

The order of the checks is the spec's (FR-2's flow through FR-3.3): the day's allowance first, then
concurrency, then bandwidth, each refusing on its own terms and each pointing at ``/quota``. A
reservation taken and then abandoned (a refused slot) is *released*, which is why every early return
after :func:`app.quota.reserve_download` undoes it — otherwise a user's day would shrink every time
they hit a limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pyrogram.types import Message

from app import catalog, config, quota, users
from app.db import session as db_session
from app.queue import counts as job_counts
from app.queue import enqueue
from app.tiers import get_limits

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Accepted:
    """The job was queued and its status message is posted."""

    job: Any
    status_message: Any


@dataclass(frozen=True)
class Refused:
    """The job was not queued. ``text`` is what the user is told; ``reason`` is for the log."""

    text: str
    reason: str


async def submit(
    message: Message,
    *,
    url: str,
    site: str | None = None,
) -> Accepted | Refused:
    """Reserve, check, queue, and post the "Queuing download…" status message (FR-3.4/3.5)."""
    settings = config.get_settings()
    sender = message.from_user
    assert sender is not None  # every caller is a private-chat command, so this holds

    async with db_session() as db:
        user = await users.ensure_registered(
            db,
            user_id=sender.id,
            username=sender.username,
            first_name=sender.first_name,
        )
        tier = users.tier_for(settings, user)
        limits = get_limits(settings, tier.name)

        usage = await quota.current(db, sender.id)
        credits = await quota.active_bonus_credits(db, sender.id)
        allowance = quota.download_allowance(
            settings, tier_daily_downloads=limits.daily_downloads, credits=credits
        )
        bonus = quota.bonus_allowance(settings, credits)

        if not await quota.reserve_download(db, sender.id, limit=allowance, bonus=credits):
            return Refused(
                text=catalog.quota_message(
                    tier, usage, bonus=bonus, slots_active=0, queued=0
                ),
                reason="daily allowance reached",
            )

        counts = await job_counts(db, sender.id)
        if counts.running >= limits.concurrent:
            await quota.release_download(db, sender.id)
            return Refused(
                text=catalog.quota_message(
                    tier,
                    usage,
                    bonus=bonus,
                    slots_active=counts.running,
                    queued=counts.queued + 1,
                ),
                reason="no free concurrency slot",
            )

        if quota.bandwidth_exhausted(usage, limits.daily_mb):
            await quota.release_download(db, sender.id)
            return Refused(
                text=catalog.quota_message(
                    tier, usage, bonus=bonus, slots_active=counts.running, queued=counts.queued
                ),
                reason="daily bandwidth exhausted",
            )

        job = await enqueue(
            db,
            user_id=sender.id,
            link=url,
            tier=tier.name,
            priority=limits.priority,
            site=site,
        )
        await db.commit()
        position = counts.queued + 1
        job_id = job.id

    status_message = await message.reply_text(
        catalog.QUEUING_DOWNLOAD.format(id=job_id, link=url, position=position)
    )

    # FR-7.1: the job's one status message is this one, so the renderer needs its id. The chat
    # comes from the command's own message rather than the send's return value: in a private chat
    # they are the same id, and unlike the reply it is always there.
    chat_id = getattr(getattr(message, "chat", None), "id", None) or sender.id
    async with db_session() as db:
        stored = await db.get(type(job), job_id)
        if stored is not None:
            stored.status_chat_id = chat_id
            stored.status_message_id = getattr(status_message, "id", None)
            await db.commit()

    return Accepted(job=job, status_message=status_message)
