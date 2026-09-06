"""
User-facing command handlers: /start, /help, /dl, /dt, /cancel, etc.
"""

from __future__ import annotations

from telethon import TelegramClient, events
from telethon.tl.custom import Button

import structlog
from core import config, sessions
from core.config import get_settings, is_admin
from core.db import session_scope
from core.models import User, get_or_create_user
from core.quota import get_snapshot
from core.referral import referral_stats
from core.tiers import (
    ALL_SITES, HEAVY_SITES, SOCIAL_SITES, VIDEO_HOST_SITES,
    tier_policy, size_limit,
)
from core.worker import DownloadManager
from utils import paths

logger = structlog.get_logger(__name__)

# ── pricing / copy ────────────────────────────────────────────────────────────

_FREE_NOTE = "🆓 Free tier: one file at a time, size limit per download."
_PRO_NOTE = (
    "✨ Pro — ⬆️ higher limits + faster queues.\n"
    "    💰 Upgrade: /upgrade"
)
_PREMIUM_NOTE = (
    "💎 Premium — ⬆️ highest limits + priority queue.\n"
    "    💰 Upgrade: /upgrade"
)

_HELP_HEADER = (
    "**Welcome to SpideyBot**\n"
    "Download videos, files, and media from 30+ platforms — including Terabox, "
    "YouTube, Reddit, Instagram, and more.\n"
)
_HELP_COMMANDS = """
**Essential commands**

    /start  — Sign up or refresh your status
    /help   — Show this help guide
    /dl *URL*  — Download a file
    /dt *URL*  — Download and auto-split into 1 GB chunks
    /cancel *ID*  — Cancel a queued download
    /status — View your active downloads
    /account — Account & session status
    /quota — View your usage & limits
    /sites — List supported platforms
    /referral — Get your invite link & stats
"""
_HELP_NOTES = """
**Notes**

    • One file at a time. Large files are split automatically if needed.
    • Terabox links require a logged-in account — use /account to manage sessions.
"""
_HELP_ADMIN = """

**Admin only**

    /stats  — View live download statistics
    /addpremium — Grant Pro to a user
    /removepremium — Revoke Pro from a user
    /checkpremium — Check a user's premium status
"""

# ── helper: has_premium_access ──────────────────────────────────────────────────


async def has_premium_access(user_id: int) -> bool:
    """Return True if user is admin or has pro/premium tier."""
    if is_admin(user_id):
        return True
    async with session_scope() as session:
        user = await session.get(User, user_id)
        return user is not None and user.tier in ("pro", "premium")


async def _tier_label(user_id: int) -> str:
    if is_admin(user_id):
        return "👑 Admin"
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if user and user.tier in ("pro", "premium"):
            return f"✨ {user.tier.title()}"
    return "👤 Free"


async def _session_badge(user_id: int) -> str:
    if sessions.is_client_active(user_id):
        return "🟢 Connected"
    if sessions.has_session(user_id):
        return "🟡 Session saved (not running)"
    return "🔴 No session"


# ── /start ──────────────────────────────────────────────────────────────────────

async def start_handler(event):
    user_id = event.sender_id
    username = event.sender.username

    async with session_scope() as session:
        user = await get_or_create_user(session, user_id, username)

    badge = await _tier_label(user_id)
    lines = [
        f"**Welcome, {event.sender.first_name}!**",
        "",
        badge,
        "",
    ]
    if user.tier == "free":
        lines.append(_FREE_NOTE)
    lines.append(_HELP_COMMANDS)
    buttons = [
        [Button.url("⭐ Upgrade", "https://t.me/SpideyBot?start=upgrade")],
    ]
    await event.respond("\n".join(lines), buttons=buttons)
    raise events.StopPropagation


# ── /help ───────────────────────────────────────────────────────────────────────

async def help_handler(event):
    user_id = event.sender_id
    username = event.sender.username

    async with session_scope() as session:
        user = await get_or_create_user(session, user_id, username)

    tier_label = await _tier_label(user_id)
    _, size_label = size_limit(user.tier, is_admin(is_admin(user_id)))
    session_label = await _session_badge(user_id)

    lines = [
        _HELP_HEADER,
        f"    Tier: {tier_label}",
        f"    Per-file limit: {size_label}",
        f"    Sessions: {session_label}",
        "",
        _HELP_COMMANDS,
        _HELP_NOTES,
    ]
    if is_admin(user_id):
        lines.append(_HELP_ADMIN)
    await event.respond("\n".join(lines))
    raise events.StopPropagation


# ── /dl ─────────────────────────────────────────────────────────────────────────

async def dl_handler(event):
    """Handle /dl URL — download and upload a file."""
    args = event.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await event.respond("**Usage:** /dl *URL*\n\nProvide a link to download.")
        raise events.StopPropagation

    link = args[1].strip()
    user_id = event.sender_id

    async with session_scope() as session:
        user = await get_or_create_user(session, user_id, event.sender.username)
        tier = user.tier if user.tier in ("free", "pro", "premium") else "free"

    status_msg = await event.respond("⏳ **SpideyBot:** Queuing download…")
    entry_id, task = await _manager.add_task(
        user_id, event, link,
        is_premium=user.tier in ("pro", "premium"),
        is_admin=is_admin(user_id),
        status_msg=status_msg,
    )
    pos = _manager.get_queue_position(entry_id)
    if pos > 0:
        await status_msg.edit(f"📋 Queued — position **#{pos}**. Send `/cancel {entry_id}` to abort.")
    raise events.StopPropagation


# ── /dt ─────────────────────────────────────────────────────────────────────────

async def dt_handler(event):
    """Handle /dt URL — download with Telegram split (1 GB chunks)."""
    args = event.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await event.respond("**Usage:** /dt *URL*\n\nProvide a link to download (split into 1 GB chunks).")
        raise events.StopPropagation

    link = args[1].strip()
    user_id = event.sender_id

    async with session_scope() as session:
        user = await get_or_create_user(session, user_id, event.sender.username)
        tier = user.tier if user.tier in ("free", "pro", "premium") else "free"

    status_msg = await event.respond("⏳ **SpideyBot:** Queuing download…")
    entry_id, task = await _manager.add_task(
        user_id, event, link,
        is_premium=user.tier in ("pro", "premium"),
        is_admin=is_admin(user_id),
        status_msg=status_msg,
    )
    pos = _manager.get_queue_position(entry_id)
    if pos > 0:
        await status_msg.edit(f"📋 Queued — position **#{pos}**. Send `/cancel {entry_id}` to abort.")
    raise events.StopPropagation


# ── /cancel ─────────────────────────────────────────────────────────────────────

async def cancel_handler(event):
    args = event.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await event.respond("**Usage:** /cancel *entry_id*\n\nView your active downloads with /status.")
        raise events.StopPropagation

    entry_id = args[1].strip()
    user_id = event.sender_id

    task = _manager.cancel_task(entry_id)
    if task and task.user_id == user_id:
        await event.respond(f"❌ Cancelled download **{entry_id}**.")
    elif task:
        await event.respond("⚠️ That download belongs to another user.")
    else:
        await event.respond("⚠️ No active download found with that ID.")
    raise events.StopPropagation


# ── /status ─────────────────────────────────────────────────────────────────────

async def status_handler(event):
    tasks = _manager.user_tasks(event.sender_id)
    if not tasks:
        await event.respond("✅ No active downloads.")
        raise events.StopPropagation
    lines = ["**Active downloads:**\n"]
    for t in tasks:
        status = "🔄 Running" if not t.is_cancelled else "❌ Cancelled"
        lines.append(f"  `{t.entry_id}` — {t.link[:50]}… — {status}")
    await event.respond("\n".join(lines))
    raise events.StopPropagation


# ── /quota ─────────────────────────────────────────────────────────────────────

async def quota_handler(event):
    """Show user's current usage and tier limits."""
    user_id = event.sender_id
    async with session_scope() as session:
        user = await get_or_create_user(session, user_id, event.sender.username)
        snap = await get_snapshot(session, user)

    tier_label = await _tier_label(user_id)

    def _fmt_bytes(n: int | None) -> str:
        if n is None:
            return "∞"
        if n >= 1024 ** 3:
            return f"{n / 1024**3:.1f} GB"
        if n >= 1024 ** 2:
            return f"{n / 1024**2:.0f} MB"
        return f"{n} B"

    lines = [
        "**Quota Status**\n",
        f"  Tier: {tier_label}",
    ]

    # Daily downloads
    if snap.policy.daily_downloads is not None:
        rem = snap.downloads_remaining
        lines.append(f"  Downloads today: {snap.downloads_today}/{snap.policy.daily_downloads}"
                     + (f" (+{snap.referral_bonus} bonus)" if snap.referral_bonus else "")
                     + (f"  — {rem} left" if rem is not None else ""))
    else:
        lines.append(f"  Downloads today: {snap.downloads_today} (∞)")

    # Daily bandwidth
    if snap.policy.daily_bytes is not None:
        lines.append(f"  Bandwidth today: {_fmt_bytes(snap.bytes_today)} / {_fmt_bytes(snap.policy.daily_bytes)}")
    else:
        lines.append(f"  Bandwidth today: {_fmt_bytes(snap.bytes_today)} (∞)")

    # Monthly bandwidth
    if snap.policy.monthly_bytes is not None:
        lines.append(f"  Bandwidth month: {_fmt_bytes(snap.bytes_this_month)} / {_fmt_bytes(snap.policy.monthly_bytes)}")
    else:
        lines.append(f"  Bandwidth month: {_fmt_bytes(snap.bytes_this_month)} (∞)")

    lines.append(f"  Concurrent: {snap.policy.concurrent}")
    lines.append(f"  Per-file limit: {size_limit(snap.tier, is_admin(user_id))[1]}")

    if not snap.can_download:
        lines.append("\n⚠️ **Quota reached** — try again tomorrow.")

    await event.respond("\n".join(lines))
    raise events.StopPropagation


# ── /sites ─────────────────────────────────────────────────────────────────────

async def sites_handler(event):
    """List supported sites grouped by category."""
    user_id = event.sender_id
    async with session_scope() as session:
        user = await get_or_create_user(session, user_id, event.sender.username)
    tier = user.tier if user.tier in ("free", "pro", "premium") else "free"
    policy = tier_policy(tier)

    def _label(sites: frozenset[str]) -> str:
        return ", ".join(sorted(sites))

    social = _label(SOCIAL_SITES & ALL_SITES)
    video = _label(VIDEO_HOST_SITES & ALL_SITES)
    heavy = _label(HEAVY_SITES & ALL_SITES)

    lines = [
        "**Supported Platforms**\n",
        f"  Your tier: **{tier.title()}**\n",
        f"  📱 Social & video: {social}",
        f"  🎬 Video hosting: {video}",
        f"  📦 Heavy / cloud: {heavy}",
        "",
        "Send any link with /dl or /dt to download.",
    ]
    await event.respond("\n".join(lines))
    raise events.StopPropagation


# ── /referral ───────────────────────────────────────────────────────────────────

async def referral_handler(event):
    """Show referral link and stats."""
    user_id = event.sender_id
    async with session_scope() as session:
        user = await get_or_create_user(session, user_id, event.sender.username)
        stats = await referral_stats(session, user_id)

    settings = get_settings()
    lines = [
        "**Referral Program**\n",
        f"  🔗 Your link: {stats['link']}",
        f"  👥 Invited: {stats['total']}",
        f"  ✅ Credited: {stats['credited']}",
        f"  ⏳ Pending: {stats['pending']}",
        "",
        f"  💰 Bonus: +{settings.referral_daily_bonus} downloads/day per credited invite",
        f"     (valid for {settings.referral_bonus_days} days after credit)",
    ]
    await event.respond("\n".join(lines))
    raise events.StopPropagation


# ── /account ────────────────────────────────────────────────────────────────────

async def account_handler(event):
    user_id = event.sender_id
    badge = await _session_badge(user_id)
    lines = [
        "**Account Status**\n",
        f"    {badge}\n",
        "To log in to Terabox, use /login.",
        "To log out, use /logout.",
    ]
    await event.respond("\n".join(lines))
    raise events.StopPropagation


# ── registration ────────────────────────────────────────────────────────────────

_manager: DownloadManager


def register_user_handlers(client: TelegramClient, manager: DownloadManager) -> None:
    global _manager
    _manager = manager
    client.add_event_handler(start_handler, events.NewMessage(pattern=r"/start"))
    client.add_event_handler(help_handler, events.NewMessage(pattern=r"/help"))
    client.add_event_handler(dl_handler, events.NewMessage(pattern=r"/dl"))
    client.add_event_handler(dt_handler, events.NewMessage(pattern=r"/dt"))
    client.add_event_handler(cancel_handler, events.NewMessage(pattern=r"/cancel"))
    client.add_event_handler(status_handler, events.NewMessage(pattern=r"/status"))
    client.add_event_handler(account_handler, events.NewMessage(pattern=r"/account"))
    client.add_event_handler(quota_handler, events.NewMessage(pattern=r"/quota"))
    client.add_event_handler(sites_handler, events.NewMessage(pattern=r"/sites"))
    client.add_event_handler(referral_handler, events.NewMessage(pattern=r"/referral"))
