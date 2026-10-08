"""``/cancel <ID>`` — stop a job the caller owns (FR-11, AC-5).

The command only *asks*. Whether that stops a job immediately (it was still waiting) or records
a request for the worker to act on (it was mid-transfer) is decided in :mod:`app.queue`, and
this handler just reports which of those happened. Keeping that split means the refusals the
spec requires — invalid, unknown, someone else's — are decided once, in one place, with the row
in hand, rather than by re-reading state here and hoping it has not changed.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog, queue, users
from app.db import session as db_session


def cancel_argument(message: Message) -> str | None:
    """The command's first argument, if it was given.

    Kurigram has already split the command, so ``/cancel 42`` arrives as ``["cancel", "42"]``.
    """
    command = getattr(message, "command", None) or []
    return command[1] if len(command) > 1 else None


@Client.on_message(filters.command("cancel") & filters.private)
async def cancel_command(client: Client, message: Message) -> None:
    sender = message.from_user
    if sender is None:
        return

    raw = cancel_argument(message)
    if raw is None:
        await message.reply_text(catalog.USAGE_CANCEL)
        return

    if not raw.isdigit():
        # Also covers a negative or a float: neither can be a job id, and the answer is the
        # same one FR-11.2 asks for — point the user at where real ids are listed.
        await message.reply_text(catalog.CANCEL_INVALID_ID)
        return

    job_id = int(raw)
    async with db_session() as db:
        # Registered first so the ownership check compares against a real user row rather than
        # silently refusing a caller the bot has never seen.
        await users.ensure_registered(db, user_id=sender.id)
        outcome = await queue.request_cancel(db, job_id=job_id, user_id=sender.id)

    await message.reply_text(catalog.cancel_message(kind=outcome.kind, job_id=job_id))
