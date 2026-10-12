"""``/dt`` — download from a Telegram message link (FR-4, AC-7).

Three things happen here, in the order FR-4 asks for them:

1. the link is **parsed**, and a link that is not a Telegram one gets the refusal that points at
   ``/dl`` (FR-4.1) while a malformed one gets the parser's own sentence (FR-4.6);
2. the **session** is checked *before* anything is queued (FR-4.2). A queued job that cannot run is
   worse than a refusal: the user has spent a download from their day and is waiting for a file that
   will never arrive;
3. the job is queued through the same admission path as ``/dl`` (FR-3), so Telegram downloads count
   against quota and concurrency exactly like every other download (AC-10).

The fetch itself is the worker's job (``app.jobs``): resolving here would mean downloading inside a
handler, which is the pipeline's work and the queue's business.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog
from handlers import submission
from telegram import links
from telegram.sessions import SessionError, get_pool


@Client.on_message(filters.command("dt") & filters.private)
async def dt_command(client: Client, message: Message) -> None:
    sender = message.from_user
    if sender is None:
        return

    command = getattr(message, "command", None) or []
    raw = " ".join(command[1:]).strip()
    if not raw:
        await message.reply_text(catalog.USAGE_DT)
        return

    try:
        target = links.parse(raw)
    except links.TgLinkError as invalid:
        # FR-4.1 vs FR-4.6: the parser's sentence already says which of the two this is, so the
        # only addition needed here is the pointer to the command that *does* take other links.
        await message.reply_text(
            str(invalid) if _is_telegram_link(raw) else catalog.DT_NOT_TELEGRAM
        )
        return

    pool = get_pool()
    if not pool.has_session(sender.id) and not pool.is_live(sender.id):
        await message.reply_text(catalog.DT_NO_SESSION)
        return
    try:
        await pool.get(sender.id)
    except SessionError as refused:
        # A stored session that no longer works: the pool has already deleted it, so the answer is
        # the same one an absent session gets, with the reason attached.
        await message.reply_text(
            catalog.DT_NO_SESSION if refused.expired else catalog.DT_SESSION_EXPIRED.format(
                reason=refused.reason
            )
        )
        return

    url = _canonical(target)
    outcome = await submission.submit(message, url=url, site="telegram")
    if isinstance(outcome, submission.Refused):
        await message.reply_text(outcome.text)


def _is_telegram_link(raw: str) -> bool:
    """Whether the text looks like it was *meant* to be a Telegram link.

    Used only to choose between two refusals: a link to another site deserves "use /dl", while a
    Telegram link with a typo deserves the parser's more specific complaint.
    """
    lowered = raw.lower()
    return any(host in lowered for host in links.HOSTS)


def _canonical(target: links.TgTarget) -> str:
    """The job's link, stored in one spelling so the two ends of a range are both kept.

    A range is stored as ``start-end`` with the two message links: the worker reads it back through
    the same parser, so the job row alone is enough to reproduce the request — which is what makes a
    job survivable across a restart.
    """
    if isinstance(target, links.RangeLink):
        return f"{_message_url(target.start)}-{_message_url(target.end)}"
    if isinstance(target, links.FolderLink):
        return f"https://t.me/addlist/{target.slug}"
    return _message_url(target)


def _message_url(link: links.MessageLink) -> str:
    """A message link in the form the parser accepts back, topics included."""
    if link.private:
        base = f"https://t.me/c/{link.chat}"
    else:
        base = f"https://t.me/{link.chat}"
    if link.topic_id is not None:
        return f"{base}/{link.topic_id}/{link.message_id}"
    return f"{base}/{link.message_id}"
