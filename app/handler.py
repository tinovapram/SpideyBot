"""Class-based command handlers.

V5 pattern: ``Handler`` owns both sides of the bot — slash commands on the shared
bot client via ``register_bot_handlers``, and ``/dl``/``/dt`` on a user's own
client via ``register_user_handlers``.

Call :func:`install` once at startup. User clients reach that instance through
:func:`get_handler`, which keeps a single ``DownloadManager`` behind both sides.
"""

from __future__ import annotations

from typing import Any

from pyrogram import Client, filters
from pyrogram.types import Message

from app import catalog
from app.config import get_settings
from app.manager import DownloadManager

_handler: "Handler | None" = None


def get_handler() -> "Handler":
    """Return the single :class:`Handler` instance, creating it if needed."""
    global _handler
    if _handler is None:
        from app.runtime import _manager  # type: ignore[attr-defined]

        if _manager is None:
            raise RuntimeError("DownloadManager not installed yet")
        _handler = Handler(bot=_manager.bot, manager=_manager)
    return _handler


def install(bot: Client, manager: DownloadManager) -> None:
    """Install the global handler once at startup."""
    global _handler
    if _handler is not None:
        return
    _handler = Handler(bot=bot, manager=manager)
    _handler.register_bot_handlers(bot)


class Handler:
    """Bot-side command handlers plus the user-side auto-download handlers."""

    def __init__(self, bot: Client, manager: DownloadManager) -> None:
        self.bot = bot
        self.manager = manager

    # ── download plumbing ───────────────────────────────────────

    async def _enqueue_download(self, event: Message, link: str) -> None:
        """Common entry for /dl and /dt from either bot or user client."""
        user_id = event.from_user.id if event.from_user else 0
        settings = get_settings()
        is_admin = user_id in settings.admin_ids

        # Post an initial status message
        status_msg = await event.reply_text("📥 Queuing download…")

        # Enqueue via manager
        _status, task = await self.manager.add_task(
            user_id=user_id,
            event=event,
            link=link,
            is_premium=False,
            is_admin=is_admin,
            status_msg=status_msg,
        )

        await status_msg.edit_text(f"✅ Queued job `#{task.job_id}` — use /status to check progress.")

    # ── /start ──────────────────────────────────────────────────

    async def start_handler(self, client: Client, message: Message) -> None:
        await message.reply_text(catalog.WELCOME_TEXT)

    # ── /help ───────────────────────────────────────────────────

    async def help_handler(self, client: Client, message: Message) -> None:
        await message.reply_text(catalog.HELP_TEXT)

    # ── /dl ─────────────────────────────────────────────────────

    async def dl_handler(self, client: Client, message: Message) -> None:
        if message.from_user is None:
            return
        url = _command_argument(message)
        if not url:
            await message.reply_text(catalog.USAGE_DL)
            return
        await self._enqueue_download(message, url)

    # ── /dt ─────────────────────────────────────────────────────

    async def dt_handler(self, client: Client, message: Message) -> None:
        if message.from_user is None:
            return
        url = _command_argument(message)
        if not url:
            await message.reply_text(catalog.USAGE_DT)
            return
        await self._enqueue_download(message, url)

    # ── /cancel ─────────────────────────────────────────────────

    async def cancel_handler(self, client: Client, message: Message) -> None:
        if message.from_user is None:
            return
        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2 or not args[1].strip():
            await message.reply_text(catalog.USAGE_CANCEL)
            return

        raw = args[1].strip()
        if not raw.isdigit():
            await message.reply_text("⚠️ Invalid ID — pass the number shown by /status.")
            return

        entry_id = int(raw)
        task = self.manager.get_task(entry_id)
        if task is None:
            await message.reply_text(f"⚠️ No active download with ID `{entry_id}`.")
        elif task.user_id != message.from_user.id:
            await message.reply_text("⚠️ That download belongs to another user.")
        else:
            await self.manager.cancel_task(entry_id)
            await message.reply_text(f"❌ Cancelled download **{entry_id}**.")

    # ── /status ─────────────────────────────────────────────────

    async def status_handler(self, client: Client, message: Message) -> None:
        if message.from_user is None:
            return
        tasks = self.manager.user_tasks(message.from_user.id)
        if not tasks:
            await message.reply_text("✅ No active downloads.")
            return

        lines = ["**Active downloads:**\n"]
        for task in tasks:
            state = "❌ Cancelled" if task.is_cancelled else "🔄 Running"
            lines.append(f"  `#{task.job_id}` — {state}")
        await message.reply_text("\n".join(lines))

    # ── /account ────────────────────────────────────────────────

    async def account_handler(self, client: Client, message: Message) -> None:
        await message.reply_text("👤 Account commands: /login, /logout")

    # ── /quota ──────────────────────────────────────────────────

    async def quota_handler(self, client: Client, message: Message) -> None:
        await message.reply_text("📊 Use /quota_info to check your quota.")

    # ── /sites ──────────────────────────────────────────────────

    async def sites_handler(self, client: Client, message: Message) -> None:
        await message.reply_text("🌐 Supported sites: YouTube, TikTok, Instagram, Twitter, TeraBox, …")

    # ── /referral ───────────────────────────────────────────────

    async def referral_handler(self, client: Client, message: Message) -> None:
        await message.reply_text("🔗 Referral: invite friends to earn bonus downloads.")

    # ── registration ────────────────────────────────────────────

    def register_bot_handlers(self, client: Client) -> None:
        """Register the slash commands on the shared bot client."""
        add = client.add_handler

        from pyrogram.handlers import MessageHandler

        add(MessageHandler(self.start_handler, filters.command("start") & filters.private))
        add(MessageHandler(self.help_handler, filters.command("help") & filters.private))
        add(MessageHandler(self.dl_handler, filters.command("dl") & filters.private))
        add(MessageHandler(self.dt_handler, filters.command("dt") & filters.private))
        add(MessageHandler(self.cancel_handler, filters.command("cancel") & filters.private))
        add(MessageHandler(self.status_handler, filters.command("status") & filters.private))
        add(MessageHandler(self.account_handler, filters.command("account") & filters.private))
        add(MessageHandler(self.quota_handler, filters.command("quota") & filters.private))
        add(MessageHandler(self.sites_handler, filters.command("sites") & filters.private))
        add(MessageHandler(self.referral_handler, filters.command("referral") & filters.private))

    def register_user_handlers(self, client: Client) -> None:
        """Attach /dl and /dt on a user's own client."""
        add = client.add_handler

        from pyrogram.handlers import MessageHandler

        add(MessageHandler(self.dl_handler, filters.command("dl") & filters.private))
        add(MessageHandler(self.dt_handler, filters.command("dt") & filters.private))


def _command_argument(message: Message) -> str:
    """Everything after the command name, as one string."""
    command = getattr(message, "command", None) or []
    return " ".join(command[1:]).strip()
