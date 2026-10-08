"""``/status`` — the caller's active downloads (FR-12, AC-6).

Named ``job_status`` rather than ``status`` so that a search for "status" does not return this
file and the status-message view together; the view belongs to ``telegram/status.py``.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog, queue, users
from app.db import session as db_session


@Client.on_message(filters.command("status") & filters.private)
async def status_command(client: Client, message: Message) -> None:
    sender = message.from_user
    if sender is None:
        return

    async with db_session() as db:
        await users.ensure_registered(db, user_id=sender.id)
        jobs = await queue.active_jobs(db, sender.id)

    lines = [
        catalog.JobLine(
            id=job.id,
            link=catalog.truncate_link(job.link),
            state=catalog.job_state(
                status=job.status,
                cancel_requested=job.cancel_requested_at is not None,
            ),
        )
        for job in jobs
    ]
    await message.reply_text(catalog.status_message(lines))
