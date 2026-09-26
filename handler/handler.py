"""
User-facing command handlers: /start, /help, /dl, /dt, /cancel, /status,
/account, /quota, /sites, /referral.

``Handler`` owns both sides of the bot:

* :meth:`Handler.register_bot_handlers` — the slash commands, on the shared bot
  client.
* :meth:`Handler.register_user_handlers` — ``/dl`` and ``/dt`` on a *user's own*
  client (see ``core.sessions``).

``/dl`` and ``/dt`` therefore exist twice. A live user session is the priority
path: the user's client sees the command as an outgoing message and runs it, so
the bot handler checks :func:`core.sessions.is_client_active` and stands down.
Without that check the same link would be queued twice.

Call :func:`install` once at startup. User clients reach that instance through
:func:`get_handler`, which keeps a single ``DownloadManager`` behind both sides.
"""

from __future__ import annotations

import re

from click import Path
from telethon import TelegramClient, events
from telethon.tl.custom import Button

import structlog
from core import sessions
from core.config import get_settings, is_admin
from core.db import session_scope
from core.models import User, get_or_create_user
from core.quota import check_quota, get_snapshot, QuotaExceeded
from core.referral import referral_stats
from core.tiers import (
    HEAVY_SITES, size_limit,
)
from core.worker import DownloadManager, _MessageStub
from downloader.telegram import (
    download_tg_message,
    download_tg_range,
    is_tg_range_url,
    parse_tg_link,
    parse_tg_range,
)
from utils.files import prepare_media
from utils.paths import DOWNLOADS_DIR
from utils.progress import StatusMessage

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
    /bypass *URL* — Resolve link shorteners to final destination
    /referral — Get your invite link & stats
"""
_HELP_NOTES = """
**Notes**

    • One file at a time. Large files are split automatically if needed.
    • Terabox links require a logged-in account — use /account to manage sessions.
    • Reply to media and send your magic word (default: `WOW`) to download it.
    • Private photos/videos are auto-saved — manage via /setting.
"""
_HELP_ADMIN = """

**Admin only**

    /stats  — View live download statistics
    /addpremium — Grant Pro to a user
    /removepremium — Revoke Pro from a user
    /checkpremium — Check a user's premium status
"""

_USAGE_DL = "**Usage:** /dl *URL*\n\nProvide a link to download."
_USAGE_DT = "**Usage:** /dt *Telegram link*\n\nDownload directly from a Telegram message (t.me link). Requires an active session — use /start or /login."
_USAGE_CANCEL = "**Usage:** /cancel *ID*\n\nView your active downloads with /status."


def _sender_username(event) -> str | None:
    """Sender's @username, or ``None`` for private accounts."""
    sender = event.sender
    return getattr(sender, "username", None) if sender else None


_TG_LINK_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/")


def _is_telegram_link(url: str) -> bool:
    """Return True when *url* is a Telegram message/channel link."""
    return bool(_TG_LINK_RE.search(url))


class Handler:
    """Bot-side command handlers plus the user-side auto-download handlers."""

    def __init__(self, client: TelegramClient, manager: DownloadManager) -> None:
        self.client = client
        self.manager = manager
        self._bot_username: str | None = None

    # ── shared helpers ────────────────────────────────────────────────────────

    async def has_premium_access(self, user_id: int) -> bool:
        """True when *user_id* is an admin or has pro/premium tier."""
        if is_admin(user_id):
            return True
        async with session_scope() as session:
            user = await session.get(User, user_id)
            return user is not None and user.tier in ("pro", "premium")

    async def _tier_label(self, user_id: int) -> str:
        if is_admin(user_id):
            return "👑 Admin"
        async with session_scope() as session:
            user = await session.get(User, user_id)
            if user and user.tier in ("pro", "premium"):
                return f"✨ {user.tier.title()}"
        return "👤 Free"

    async def _session_badge(self, user_id: int) -> str:
        if sessions.is_client_active(user_id):
            return "🟢 Connected"
        if sessions.has_session(user_id):
            return "🟡 Session saved (not running)"
        return "🔴 No session"

    async def _resolve_bot_username(self) -> str | None:
        """Return the bot's own @username, cached for the process lifetime.

        ``get_me()`` is a network round-trip and the username is fixed while the
        bot runs, so resolve it once. Failures aren't cached, so they retry.
        """
        if self._bot_username is None:
            me = await self.client.get_me()
            self._bot_username = (getattr(me, "username", None) or None) if me else None
        return self._bot_username

    # ── download plumbing ─────────────────────────────────────────────────────

    async def _enqueue_download(self, event, link: str) -> None:
        """Queue *link* for the sender and report back the queue position."""
        user_id = event.sender_id
        async with session_scope() as session:
            user = await get_or_create_user(session, user_id, _sender_username(event))
            is_premium = user.is_admin or user.tier in ("pro", "premium")
            try:
                await check_quota(session, user)
            except QuotaExceeded as exc:
                detail = ""
                snap = exc.snapshot
                if snap and snap.policy.concurrent <= snap.active_downloads:
                    detail = f" ({snap.active_downloads}/{snap.policy.concurrent} slots used)"
                await event.client.send_message(
                    event.chat_id,
                    f"🚫 **SpideyBot:** {exc}{detail}\nSend /quota to check limits.",
                    reply_to=event.message,
                )
                return

        status_msg = await event.client.send_message(event.chat_id, "⏳ **SpideyBot:** Queuing download…",reply_to=event.message)
        # add_task returns (status, task) — the id lives on the task.
        _, task = await self.manager.add_task(
            user_id, event, link,
            is_premium=is_premium,
            is_admin=is_admin(user_id),
            status_msg=status_msg,
        )
        position = self.manager.get_queue_position(task.entry_id)
        if position > 0:
            await status_msg.edit(
                f"📋 Queued — position **#{position}**. "
                f"Send `/cancel {task.entry_id}` to abort."
            )

    async def _link_command(self, event, *, usage: str) -> None:
        """Shared /dl and /dt implementation: parse the URL and enqueue it."""
        args = (event.text or "").split(maxsplit=1)
        if len(args) < 2 or not args[1].strip():
            await event.client.send_message(event.chat_id, usage,reply_to=event.message)
            raise events.StopPropagation
        await self._enqueue_download(event, args[1].strip())
        raise events.StopPropagation

    async def _download_tg(self, event, link: str) -> None:
        """Download from Telegram message link using the user's own client."""
        from telethon.errors import RPCError as TelethonRPCError

        is_range = is_tg_range_url(link)
        try:
            parse_tg_range(link) if is_range else parse_tg_link(link)
        except ValueError as exc:
            await event.client.send_message(event.chat_id, f"⚠️ {exc}", reply_to=event.message)
            return

        label = " (range)" if is_range else ""
        status_msg = await event.client.send_message(event.chat_id, 
            f"⏳ **SpideyBot:** Downloading from Telegram{label}...",reply_to=event.message
        )
        async def _resend_dt(text):
            """Fresh message replying to the original command."""
            msg = await event.client.send_message(event.chat_id, text, reply_to=event.message.id)
            return _MessageStub(msg.id if msg else 0, event.client, event.chat_id)
        status = StatusMessage(status_msg, _resend=_resend_dt)
        status.set_header(f"🔄 **SpideyBot:** Downloading from Telegram{label}")

        output_dir = str(DOWNLOADS_DIR / f"tg_{event.sender_id}")
        dl_cb = status.bytes_cb("tgdl", "📥", "Downloading")
        try:
            if is_range:
                result = await download_tg_range(
                    self.client, link, output_dir=output_dir,
                    progress_callback=dl_cb,
                )
            else:
                result = await download_tg_message(
                    self.client, link, output_dir=output_dir,
                    progress_callback=dl_cb,
                )
        except Exception as exc:
            logger.error("TG download failed", error=str(exc))
            result = {"ok": False, "error": str(exc)}

        if not result["ok"]:
            await status.cleanup_close(f"❌ **SpideyBot:** {result['error']}")
            return

        files = result["files"]
        metadata = result.get("file_metadata") or [None] * len(files)

        import os
        try:
            status.drop("tgdl")
            status.set_header("📤 **SpideyBot:** Uploading to Telegram")
            up_cb = status.bytes_cb("ul", "📤", "Uploading")
            media = []
            for fp, meta in zip(files, metadata):
                if not os.path.isfile(fp):
                    continue
                try:
                    kwargs = {}
                    if meta:
                        if meta.get("photo"):
                            kwargs["as_image"] = True
                        elif meta.get("animated"):
                            kwargs["supports_streaming"] = True
                            kwargs["nosound_video"] = True
                        elif meta.get("video"):
                            kwargs["supports_streaming"] = True
                        elif meta.get("force_document"):
                            kwargs["force_document"] = True
                    media.append(
                        await prepare_media(
                            self.client, fp,
                            progress_callback=up_cb, **kwargs,
                        )
                    )
                except Exception as exc:
                    logger.warning("Failed to prepare TG file", file=fp, error=str(exc))

            if media:
                await self.client.send_file(
                    event.chat_id, media,
                    caption=f"✅ **SpideyBot:** Downloaded {len(media)} file(s) from Telegram{label}.",
                    reply_to=event.message,
                )
            else:
                await event.client.send_message(event.chat_id, "✅ **SpideyBot:** Downloaded but nothing to send.")

            if is_range:
                final = (
                    f"✅ **SpideyBot:** Downloaded {result.get('downloaded_messages', len(files))} file(s) "
                    f"from {result.get('total_messages', len(files))} messages in "
                    f"`{result.get('chat_title', '')}`."
                )
            else:
                final = f"✅ **SpideyBot:** Downloaded {len(files)} file(s) from `{result.get('chat_title', '')}`."
            await status.cleanup_close(final)
        except Exception as exc:
            logger.error("Failed to send TG files", error=str(exc))
            await status.cleanup_close(f"❌ **SpideyBot:** Failed to send files: `{exc}`")
        finally:
            for fp in files:
                try:
                    os.remove(fp)
                except OSError:
                    pass
            try:
                os.rmdir(output_dir)
            except OSError:
                pass

    # ── /start ────────────────────────────────────────────────────────────────

    async def start_handler(self, event) -> None:
        user_id = event.sender_id

        async with session_scope() as session:
            user = await get_or_create_user(session, user_id, _sender_username(event))

        # Auto-start saved session so /dl and /dt work everywhere immediately.
        if sessions.has_session(user_id) and not sessions.is_client_active(user_id):
            started = await sessions.start_client(user_id)
            if started:
                logger.info("Auto-started user session from /start", user_id=user_id)

        badge = await self._tier_label(user_id)
        session_label = await self._session_badge(user_id)
        lines = [
            f"**Welcome, {event.sender.first_name}!**",
            "",
            badge,
            f"    Sessions: {session_label}",
            "",
        ]
        if user.tier == "free" and not is_admin(user_id):
            lines.append(_FREE_NOTE)
        lines.append(_HELP_COMMANDS)
        buttons = [
            [Button.url("⭐ Upgrade", "https://t.me/SpideyBot?start=upgrade")],
        ]
        await event.client.send_message(event.chat_id, "\n".join(lines), buttons=buttons)
        raise events.StopPropagation

    # ── /help ─────────────────────────────────────────────────────────────────

    async def help_handler(self, event) -> None:
        user_id = event.sender_id

        async with session_scope() as session:
            user = await get_or_create_user(session, user_id, _sender_username(event))

        tier_label = await self._tier_label(user_id)
        _, limit_label = size_limit(user.tier, is_admin(user_id))
        session_label = await self._session_badge(user_id)

        lines = [
            _HELP_HEADER,
            f"    Tier: {tier_label}",
            f"    Link limit: {limit_label}",
            f"    Sessions: {session_label}",
            "",
            _HELP_COMMANDS,
            _HELP_NOTES,
        ]
        if is_admin(user_id):
            lines.append(_HELP_ADMIN)
        await event.client.send_message(event.chat_id, "\n".join(lines))
        raise events.StopPropagation

    # ── /dl and /dt (bot side) ────────────────────────────────────────────────
    #
    # Both commands exist twice: here, and on the user's own client. When a user
    # session is live the user's client sees their own /dl as an outgoing
    # message and handles it, so the bot stands down — otherwise the same link
    # is queued twice. When no session is running there is nothing to arbitrate:
    # only the bot can see the command at all.

    def _handled_by_user_session(self, event) -> bool:
        """True when the sender has a live user client that will handle this."""
        return sessions.is_client_active(event.sender_id)

    async def dl_handler(self, event) -> None:
        """Bot-side /dl — stands down when the user's own session has it."""
        if self._handled_by_user_session(event):
            return
        await self._link_command(event, usage=_USAGE_DL)

    async def dt_handler(self, event) -> None:
        """Bot-side /dt — only for Telegram links, requires user session."""
        if self._handled_by_user_session(event):
            return
        args = (event.text or "").split(maxsplit=1)
        if len(args) < 2 or not args[1].strip():
            await event.client.send_message(event.chat_id, _USAGE_DT)
            raise events.StopPropagation
        link = args[1].strip()
        if not _is_telegram_link(link):
            await event.client.send_message(event.chat_id, 
                "⚠️ **/dt is for Telegram links only.**\n"
                "Use `/dl <URL>` for other sites, or `/dt <t.me link>` for Telegram."
            )
            raise events.StopPropagation
        # No active session — tell user to start one.
        await event.client.send_message(event.chat_id, 
            "⚠️ **Telegram downloads need your session.**\n"
            "Send /start or /login first, then use /dt again."
        )
        raise events.StopPropagation

    # ── /cancel ───────────────────────────────────────────────────────────────

    async def cancel_handler(self, event) -> None:
        args = (event.text or "").split(maxsplit=1)
        if len(args) < 2 or not args[1].strip():
            await event.client.send_message(event.chat_id, _USAGE_CANCEL)
            raise events.StopPropagation

        raw = args[1].strip()
        if not raw.isdigit():
            await event.client.send_message(event.chat_id, 
                "⚠️ **Invalid ID** — pass the number shown by /status."
            )
            raise events.StopPropagation

        entry_id = int(raw)
        task = self.manager.get_task(entry_id)
        if task is None:
            await event.client.send_message(event.chat_id, f"⚠️ No active download with ID `{entry_id}`.")
        elif task.user_id != event.sender_id:
            await event.client.send_message(event.chat_id, "⚠️ That download belongs to another user.")
        else:
            await self.manager.cancel_task(entry_id)
            await event.client.send_message(event.chat_id, f"❌ Cancelled download **{entry_id}**.")
        raise events.StopPropagation

    # ── /status ───────────────────────────────────────────────────────────────

    async def status_handler(self, event) -> None:
        tasks = self.manager.user_tasks(event.sender_id)
        if not tasks:
            await event.client.send_message(event.chat_id, "✅ No active downloads.")
            raise events.StopPropagation

        lines = ["**Active downloads:**\n"]
        for task in tasks:
            state = "❌ Cancelled" if task.is_cancelled else "🔄 Running"
            link = task.link if len(task.link) <= 50 else task.link[:50] + "…"
            lines.append(f"  `{task.entry_id}` — {link} — {state}")
        await event.client.send_message(event.chat_id, "\n".join(lines))
        raise events.StopPropagation

    # ── /account ──────────────────────────────────────────────────────────────

    async def account_handler(self, event) -> None:
        badge = await self._session_badge(event.sender_id)
        lines = [
            "**Account Status**\n",
            f"    {badge}\n",
            "To log in to Terabox, use /login.",
            "To log out, use /logout.",
        ]
        await event.client.send_message(event.chat_id, "\n".join(lines))
        raise events.StopPropagation

    # ── /quota ────────────────────────────────────────────────────────────────

    async def quota_handler(self, event) -> None:
        """Show the user's current usage and tier limits."""
        user_id = event.sender_id
        async with session_scope() as session:
            user = await get_or_create_user(session, user_id, _sender_username(event))
            snap = await get_snapshot(session, user)

        tier_label = await self._tier_label(user_id)

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

        if snap.policy.daily_downloads is not None:
            remaining = snap.downloads_remaining
            lines.append(
                f"  Downloads today: {snap.downloads_today}/{snap.policy.daily_downloads}"
                + (f" (+{snap.referral_bonus} bonus)" if snap.referral_bonus else "")
                + (f"  — {remaining} left" if remaining is not None else "")
            )
        else:
            lines.append(f"  Downloads today: {snap.downloads_today} (∞)")

        if snap.policy.daily_bytes is not None:
            lines.append(
                f"  Bandwidth today: {_fmt_bytes(snap.bytes_today)} / "
                f"{_fmt_bytes(snap.policy.daily_bytes)}"
            )
        else:
            lines.append(f"  Bandwidth today: {_fmt_bytes(snap.bytes_today)} (∞)")

        lines.append(f"  Concurrent: {snap.policy.concurrent}")
        lines.append(f"  Link limit: {size_limit(snap.tier, is_admin(user_id))[1]}")

        if not snap.can_download:
            lines.append("\n⚠️ **Quota reached** — try again tomorrow.")

        await event.client.send_message(event.chat_id, "\n".join(lines))
        raise events.StopPropagation

    # ── /sites ────────────────────────────────────────────────────────────────

    async def sites_handler(self, event) -> None:
        """List supported sites grouped by category."""
        lines = [
            "**Supported Platforms**\n",
            f"  📱 Social & video: YouTube, Twitter, TikTok, Reddit, Instagram, ...",
            f"  🎬 Video hosting: Doodstream, StreamTape, MixDrop, StreamWish, ...",
            f"  📦 Heavy / cloud: TeraBox",
            "",
            "Send any link with /dl or /dt to download.",
        ]
        await event.client.send_message(event.chat_id, "\n".join(lines))
        raise events.StopPropagation

    # ── /referral ─────────────────────────────────────────────────────────────

    async def referral_handler(self, event) -> None:
        """Show the user's invite link and referral stats."""
        user_id = event.sender_id

        bot_username = await self._resolve_bot_username()
        if not bot_username:
            await event.client.send_message(event.chat_id, 
                "⚠️ **Can't build your invite link.**\n"
                "Invite links need a public @username for the bot."
            )
            raise events.StopPropagation

        async with session_scope() as session:
            await get_or_create_user(session, user_id, _sender_username(event))
            stats = await referral_stats(session, user_id, bot_username=bot_username)

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
        await event.client.send_message(event.chat_id, "\n".join(lines))
        raise events.StopPropagation

    # ── user-side handlers (run on the user's own client) ─────────────────────
    #
    # No session check here — this *is* the session, and it is the priority
    # path. `outgoing=True` is the only filter, which keeps the client from
    # reacting to messages other people send the user.

    async def dl_user_handler(self, event) -> None:
        """User-side /dl typed from the user's own account."""
        await self._link_command(event, usage=_USAGE_DL)

    async def dt_user_handler(self, event) -> None:
        """User-side /dt — download from Telegram directly via user's client."""
        args = (event.text or "").split(maxsplit=1)
        if len(args) < 2 or not args[1].strip():
            await event.client.send_message(event.chat_id, _USAGE_DT)
            raise events.StopPropagation
        link = args[1].strip()
        if not _is_telegram_link(link):
            await event.client.send_message(event.chat_id, 
                "⚠️ **/dt is for Telegram links only.**\n"
                "Use `/dl <URL>` for other sites."
            )
            raise events.StopPropagation
        await self._download_tg(event, link)
        raise events.StopPropagation
    
    async def magic_user_handler(self, event) -> None:
        """User-side magic word — download a replied message media."""
        # Load user's custom magic word (case insensitive check)
        try:
            async with session_scope() as session:
                user = await get_or_create_user(session, event.sender_id)
                magic_word = user.magic_word
        except Exception:
            magic_word = "WOW"  # fallback to default
        text = (event.text or "").strip()
        if text.upper() != magic_word.upper():
            return  # not the magic word — silently ignore
        if not event.is_reply:
            await event.client.send_message('me', "⚠️ **Reply to a message with media and send `WOW` to download it.**")
            raise events.StopPropagation
        msg = await event.get_reply_message()
        if not msg or not msg.media:
            await event.client.send_message('me', "⚠️ **Replied message has no media.**\n" "Reply to a message with media and send `WOW` to download it.")
            raise events.StopPropagation
        status_msg = await event.client.send_message('me', "⏳ **SpideyBot:** Downloading media…")
        status = StatusMessage(status_msg)
        status.set_header("🔄 **SpideyBot:** Downloading media")
        output_dir = str(DOWNLOADS_DIR / f"tg_{event.sender_id}")
        dl_cb = status.bytes_cb("tgdl", "📥", "Downloading")
        result = None
        try:
            result = await event.client.download_media(msg, file=output_dir, progress_callback=dl_cb)
        except Exception as exc:
            logger.error("TG WOW download failed", error=str(exc))
            result = {"ok": False, "error": str(exc)}

        if not result["ok"]:
            await status.close(f"❌ **SpideyBot:** {result['error']}")
            return

        try:
            media = await prepare_media(event.client, result, progress_callback=dl_cb)
            await event.client.send_file('me', media, caption="✅ **SpideyBot:** Downloaded media from replied message.")
            await status.close("✅ **SpideyBot:** Downloaded media from replied message.")
        except Exception as exc:
            logger.error("Failed to send TG WOW media", error=str(exc))
            await status.close(f"❌ **SpideyBot:** Failed to send media: `{exc}`")  

    async def timermedia_handler(self, event: events.NewMessage.Event) -> None:
        """User-side handler for any private message with photo or video."""
        if not (event.photo or event.video):
            return
        if not event.media_unread:
            return
        me = await event.client.get_me()
        if event.sender_id == me.id:
            return
        # Check user's timer_media_enabled setting
        try:
            async with session_scope() as session:
                user = await get_or_create_user(session, me.id)
                if not user.timer_media_enabled:
                    return
        except Exception:
            pass  # if DB check fails, proceed anyway (default is ON)
        sender = await event.get_sender()
        username = f"@{sender.username}/" if sender.username else ""
        user_id = f'#id{sender.id}' if sender.id else "None"
        caption = f':\n"{event.text}"' if event.text else ""
        try:
            result = await event.download_media(
                str(Path(f"TimerMedia/from{user_id}{event.message.id}{me.id}"))
            )
        except Exception as exc:
            logger.error("Failed to download timer media", error=str(exc))
            return
        try:
            await event.client.send_message(
                "me", message=f"From {username}{user_id}{caption}", file=result
            )
        except Exception as exc:
            logger.error("Failed to send timer media", error=str(exc))
    # ── registration ──────────────────────────────────────────────────────────

    def register_bot_handlers(self) -> None:
        """Register the slash commands on the shared bot client."""
        add = self.client.add_event_handler
        # Telethon compiles string patterns with re.match, so they are already
        # anchored to the start of the text. `\b` still matters — without it
        # "/dl" would also fire on "/dload ...". `^\s*` tolerates leading space.
        add(self.start_handler, events.NewMessage(pattern=r"^\s*/start\b"))
        add(self.help_handler, events.NewMessage(pattern=r"^\s*/help\b"))
        add(self.dl_handler, events.NewMessage(pattern=r"^\s*/dl\b"))
        add(self.dt_handler, events.NewMessage(pattern=r"^\s*/dt\b"))
        add(self.cancel_handler, events.NewMessage(pattern=r"^\s*/cancel\b"))
        add(self.status_handler, events.NewMessage(pattern=r"^\s*/status\b"))
        add(self.account_handler, events.NewMessage(pattern=r"^\s*/account\b"))
        add(self.quota_handler, events.NewMessage(pattern=r"^\s*/quota\b"))
        add(self.sites_handler, events.NewMessage(pattern=r"^\s*/sites\b"))
        add(self.referral_handler, events.NewMessage(pattern=r"^\s*/referral\b"))

    def register_user_handlers(self, client: TelegramClient) -> None:
        """Attach the download commands to *client* (a user's own client).

        ``outgoing=True`` is the only filter: it stops the client reacting to
        messages other people send the user. No session check is needed — this
        client only exists while the session is live, and the bot side stands
        down for exactly that window.
        """
        client.add_event_handler(
            self.dl_user_handler, events.NewMessage(outgoing=True, pattern=r"^\s*/dl\b")
        )
        client.add_event_handler(
            self.dt_user_handler, events.NewMessage(outgoing=True, pattern=r"^\s*/dt\b")
        )
        client.add_event_handler(
            self.cancel_handler, events.NewMessage(outgoing=True, pattern=r"^\s*/cancel\b")
        )
        client.add_event_handler(
            self.status_handler, events.NewMessage(outgoing=True, pattern=r"^\s*/status\b")
        )
        client.add_event_handler(
            self.magic_user_handler, events.NewMessage(outgoing=True, pattern=r"^\s*\S+\s*$")
        )
        client.add_event_handler(
            self.timermedia_handler, events.NewMessage(func=lambda e: e.is_private and (e.photo or e.video) and e.media_unread)
        )


# ── app-wide instance ─────────────────────────────────────────────────────────

_handler: Handler | None = None


def install(client: TelegramClient, manager: DownloadManager) -> Handler:
    """Create the app-wide :class:`Handler` and return it.

    User clients reach this instance through :func:`get_handler`.
    """
    global _handler
    _handler = Handler(client, manager)
    return _handler


def get_handler() -> Handler:
    """Return the instance created by :func:`install`."""
    if _handler is None:
        raise RuntimeError("handler.install() must be called before get_handler()")
    return _handler
