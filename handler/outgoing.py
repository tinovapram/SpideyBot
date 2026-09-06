"""
Outgoing message handlers — runs on the user's own TelegramClient session.
"""

from __future__ import annotations

from telethon import TelegramClient, events

import structlog
from core import config, sessions
from core.config import get_settings, is_admin
from core.db import session_scope
from core.models import User
from core.worker import DownloadManager

logger = structlog.get_logger(__name__)

_manager: DownloadManager | None = None


def set_download_manager(manager: DownloadManager) -> None:
    """Register the download manager for outgoing handlers."""
    global _manager
    _manager = manager


async def _has_premium(user_id: int) -> bool:
    """Check if user has admin or pro/premium tier."""
    if is_admin(user_id):
        return True
    async with session_scope() as session:
        user = await session.get(User, user_id)
        return user is not None and user.tier in ("pro", "premium")


# ── handler: auto-download on user's own messages ──────────────────────────────

async def outgoing_handler(event):
    """Auto-download links sent by the user in their own chat."""
    user_id = event.sender_id
    text = event.text or ""
    link = text.strip()

    if not link or not link.startswith("http"):
        return  # ignore non-link messages

    premium = await _has_premium(user_id)
    status_msg = await event.respond("⏳ **SpideyBot:** Queuing download…")
    entry_id, task = await _manager.add_task(
        user_id, event, link,
        is_premium=premium,
        is_admin=is_admin(user_id),
        status_msg=status_msg,
    )
    pos = _manager.get_queue_position(entry_id)
    if pos > 0:
        await status_msg.edit(
            f"📋 Queued — position **#{pos}**. Send `/cancel {entry_id}` to abort."
        )


# ── registration ────────────────────────────────────────────────────────────────

def register_outgoing_handlers(client: TelegramClient, user_id: int) -> None:
    """Register handlers on a user's own TelegramClient for outgoing messages."""
    client.add_event_handler(outgoing_handler, events.NewMessage(outgoing=True))
