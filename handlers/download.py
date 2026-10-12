"""``/dl`` — generic link download (FR-3, AC-2, AC-3, AC-10).

The handler's whole job is to read the argument and hand the rest to :mod:`handlers.submission`,
which owns the quota gate and the queue entry. That split is what makes ``/dt`` and ``/dl`` the same
download as far as a user's allowance is concerned: one policy, two front doors.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog
from handlers import submission


def command_argument(message: Message) -> str:
    """Everything after the command name, as one string.

    Joined rather than taken as ``command[1]``: a URL contains no space, but an argument a user
    padded or wrapped should still be attempted rather than refused as "no link" (E1).
    """
    command = getattr(message, "command", None) or []
    return " ".join(command[1:]).strip()


@Client.on_message(filters.command("dl") & filters.private)
async def dl_command(client: Client, message: Message) -> None:
    if message.from_user is None:
        return

    url = command_argument(message)
    if not url:
        await message.reply_text(catalog.USAGE_DL)
        return

    outcome = await submission.submit(message, url=url, site=None)
    if isinstance(outcome, submission.Refused):
        await message.reply_text(outcome.text)