"""
Admin command handlers: /stats, /addpremium, /removepremium, /checkpremium.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events

import structlog
from core import config
from core.config import get_settings, is_admin
from core.db import session_scope
from core.models import User, get_or_create_user

logger = structlog.get_logger(__name__)


# ── /stats ──────────────────────────────────────────────────────────────────────

async def stats_handler(event):
    """Show live download stats (admin only)."""
    user_id = event.sender_id
    if not is_admin(user_id):
        await event.respond("⛔ Admin only.")
        raise events.StopPropagation
    try:
        from core.bot import download_manager as dm
        running = sum(1 for t in dm.active_tasks.values() if not t.is_cancelled)
        cancelled = sum(1 for t in dm.active_tasks.values() if t.is_cancelled)
        lines = [
            "**Live Stats**",
            f"  Running: {running}",
            f"  Cancelled: {cancelled}",
        ]
        await event.respond("\n".join(lines))
    except Exception as exc:
        await event.respond(f"Stats error: {exc}")
    raise events.StopPropagation


# ── premium management helpers ──────────────────────────────────────────────────

async def _add_premium_by_id(user_id: int, days: int) -> str:
    """Grant pro tier to a user by Telegram ID for *days* days."""
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if user is None:
            # create a stub user so the row exists
            user = User(id=user_id, tier="pro", tier_expiry=expires)
            session.add(user)
        else:
            user.tier = "pro"
            user.tier_expiry = expires
        await session.commit()
    return f"✅ Pro granted to `{user_id}` — expires {expires:%Y-%m-%d %H:%M UTC}"


async def _add_premium_by_username(username: str, days: int) -> str:
    """Grant pro tier to a user by @username for *days* days."""
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    clean = username.lstrip("@")
    async with session_scope() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.username == clean))
        user = result.scalar_one_or_none()
        if user is None:
            return f"❌ User @{clean} not found. They must /start the bot first."
        user.tier = "pro"
        user.tier_expiry = expires
        await session.commit()
    return f"✅ Pro granted to @{clean} (`{user.id}`) — expires {expires:%Y-%m-%d %H:%M UTC}"


async def _remove_premium(target: str) -> str:
    """Revoke pro tier, resetting the user to free."""
    # Try as numeric ID first
    try:
        uid = int(target)
        async with session_scope() as session:
            user = await session.get(User, uid)
            if user is None:
                return f"❌ User `{uid}` not found."
            user.tier = "free"
            user.tier_expiry = None
            await session.commit()
        return f"✅ Pro revoked from `{uid}` — tier reset to free."
    except ValueError:
        pass

    clean = target.lstrip("@")
    async with session_scope() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.username == clean))
        user = result.scalar_one_or_none()
        if user is None:
            return f"❌ User @{clean} not found."
        user.tier = "free"
        user.tier_expiry = None
        await session.commit()
    return f"✅ Pro revoked from @{clean} (`{user.id}`) — tier reset to free."


async def _check_premium(target: str) -> str:
    """Check and format a user's current tier status."""
    uid = None
    clean = None
    try:
        uid = int(target)
    except ValueError:
        clean = target.lstrip("@")

    async with session_scope() as session:
        if uid is not None:
            user = await session.get(User, uid)
        else:
            from sqlalchemy import select
            result = await session.execute(select(User).where(User.username == clean))
            user = result.scalar_one_or_none()

    if user is None:
        ident = f"`{uid}`" if uid else f"@{clean}"
        return f"❌ User {ident} not found."

    lines = [
        f"**User:** {user.username or 'N/A'} (`{user.id}`)",
        f"**Tier:** {user.tier}",
    ]
    if user.tier_expiry:
        if user.tier_expiry > datetime.now(timezone.utc):
            lines.append(f"**Expires:** {user.tier_expiry:%Y-%m-%d %H:%M UTC}")
        else:
            lines.append(f"**Expired:** {user.tier_expiry:%Y-%m-%d %H:%M UTC} (now free)")
    if is_admin(user.id):
        lines.append("**Admin:** 👑 Yes")
    return "\n".join(lines)


# ── /addpremium ─────────────────────────────────────────────────────────────────

async def addpremium_handler(event):
    """Usage: /addpremium <user_id_or_@username> [days=30]"""
    user_id = event.sender_id
    if not is_admin(user_id):
        await event.respond("⛔ Admin only.")
        raise events.StopPropagation

    args = event.text.split()
    if len(args) < 2:
        await event.respond("**Usage:** /addpremium `<user_id or @username>` `[days]`")
        raise events.StopPropagation

    target = args[1]
    days = int(args[2]) if len(args) > 2 else 30

    if target.lstrip("@").isdigit():
        result = await _add_premium_by_id(int(target.lstrip("@")), days)
    else:
        result = await _add_premium_by_username(target, days)

    await event.respond(result)
    raise events.StopPropagation


# ── /removepremium ──────────────────────────────────────────────────────────────

async def removepremium_handler(event):
    """Usage: /removepremium <user_id_or_@username>"""
    user_id = event.sender_id
    if not is_admin(user_id):
        await event.respond("⛔ Admin only.")
        raise events.StopPropagation

    args = event.text.split()
    if len(args) < 2:
        await event.respond("**Usage:** /removepremium `<user_id or @username>`")
        raise events.StopPropagation

    target = args[1]
    result = await _remove_premium(target)
    await event.respond(result)
    raise events.StopPropagation


# ── /checkpremium ───────────────────────────────────────────────────────────────

async def checkpremium_handler(event):
    """Usage: /checkpremium <user_id_or_@username>"""
    user_id = event.sender_id
    if not is_admin(user_id):
        await event.respond("⛔ Admin only.")
        raise events.StopPropagation

    args = event.text.split()
    if len(args) < 2:
        await event.respond("**Usage:** /checkpremium `<user_id or @username>`")
        raise events.StopPropagation

    target = args[1]
    result = await _check_premium(target)
    await event.respond(result)
    raise events.StopPropagation


# ── registration ────────────────────────────────────────────────────────────────

def register_admin_handlers(client: TelegramClient) -> None:
    client.add_event_handler(stats_handler, events.NewMessage(pattern=r"/stats"))
    client.add_event_handler(addpremium_handler, events.NewMessage(pattern=r"/addpremium"))
    client.add_event_handler(removepremium_handler, events.NewMessage(pattern=r"/removepremium"))
    client.add_event_handler(checkpremium_handler, events.NewMessage(pattern=r"/checkpremium"))
