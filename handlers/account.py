"""``/logout`` and ``/account`` — the session's life, as the user sees it (FR-13, D10, AC-7).

``/logout`` is the one place the spec's revocation rule is implemented, and the order is the rule:
``log_out()`` **first**, then delete the file (D10). Deleting only the file would leave the auth key
valid and the session sitting in the user's *Active Sessions* list, which is the opposite of what
"revoke" means. If the server call fails the file is still deleted — locally the session is gone
either way — but the user is told, because the part they asked for did not happen everywhere.

``/account`` reports the badge from two facts that are genuinely different: whether a session is
*stored* (the file) and whether it is *running* (the pool). A stopped client with a file on disk is
``🟡 Session saved (not running)``, which is exactly the state §8.3 calls for after a restart — the
next ``/start`` or any download reconnects it.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog
from telegram.sessions import SessionError, get_pool


@Client.on_message(filters.command("logout") & filters.private)
async def logout_command(client: Client, message: Message) -> None:
    sender = message.from_user
    if sender is None:
        return

    pool = get_pool()
    if not pool.has_session(sender.id) and not pool.is_live(sender.id):
        await message.reply_text(catalog.LOGOUT_NONE)
        return

    # D10: revoke server-side first. A live client is reused rather than reopened; when the session
    # is only stored, it has to be connected before it can be revoked — and a session that cannot
    # even be connected is one Telegram has already invalidated, which is not a failure to report.
    reason = ""
    try:
        session = await pool.get(sender.id)
        await session.log_out()
    except SessionError as refusal:
        reason = refusal.reason if not refusal.expired else ""
    except Exception as failed:
        reason = type(failed).__name__

    removed = await pool.forget(sender.id)
    if reason:
        await message.reply_text(catalog.LOGOUT_UNREVOKED.format(reason=reason))
    elif removed:
        await message.reply_text(catalog.LOGOUT_DONE)
    else:  # pragma: no cover - the guard above makes an empty logout unreachable
        await message.reply_text(catalog.LOGOUT_NONE)


@Client.on_message(filters.command("account") & filters.private)
async def account_command(client: Client, message: Message) -> None:
    sender = message.from_user
    if sender is None:
        return

    has_session, is_live = get_pool().badge(sender.id)
    badge = catalog.session_badge(has_session=has_session, is_live=is_live)
    await message.reply_text(catalog.ACCOUNT_STATUS.format(badge=badge))
