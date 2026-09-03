"""
SpideyBot — Bot Core.

Initialization, service setup, handler registration, and bot lifecycle.
This is the single entry point that wires everything together.
"""

import os
import signal
import asyncio
import shutil
import sys

import structlog
from telethon import TelegramClient

from spideybot import config
from spideybot import db
from spideybot.logging_config import setup_logging
from spideybot.queue_manager import DownloadQueueManager
from spideybot.downloaders.terabox_downloader import TeraBoxDownloader
from spideybot.downloaders.site_downloaders.reddit import RedditDownloader
from spideybot.core.handlers.user import register_user_handlers
from spideybot.core.handlers.admin import register_admin_handlers
from spideybot.core.handlers.login import register_login_handlers
from spideybot import user_sessions

# ─── Logging Setup ──────────────────────────────────────────────────
setup_logging()
logger = structlog.get_logger(__name__)

# ─── Telegram Client ────────────────────────────────────────────────

API_ID = config.validate_telegram_config()
logger.info("Initializing Telegram client")
bot = TelegramClient('bot_session', API_ID, config.TG_API_HASH)

# ─── Downloaders ────────────────────────────────────────────────────

terabox_downloader = None
if config.TERABOX_COOKIE:
    try:
        terabox_downloader = TeraBoxDownloader(
            cookie=config.TERABOX_COOKIE,
            js_token=config.TERABOX_JSTOKEN,
            bds_token=config.TERABOX_BDSTOKEN
        )
        logger.info("TeraBox downloader initialized")
    except Exception as e:
        logger.error("Failed to initialize TeraBox downloader", error=str(e))
else:
    logger.warning("TERABOX_COOKIE not set — TeraBox features unavailable")

reddit_downloader = None
try:
    reddit_downloader = RedditDownloader(
        client_id=config.REDDIT_PRAW_CLIENT_ID,
        client_secret=config.REDDIT_PRAW_CLIENT_SECRET,
        refresh_token=config.REDDIT_PRAW_REFRESH_TOKEN,
        refresh_token_client_id=config.GDL_REDDIT_CLIENT_ID,
        refresh_token_client_secret=config.GDL_REDDIT_CLIENT_SECRET,
    )
    logger.info("Reddit downloader initialized")
except Exception as e:
    logger.error("Failed to initialize Reddit downloader", error=str(e))

# ─── Queue Manager ──────────────────────────────────────────────────

download_queue_manager = DownloadQueueManager(
    bot, terabox_downloader, reddit_downloader,
    max_concurrent=config.MAX_CONCURRENT_DOWNLOADS
)

# ─── Register Handlers ──────────────────────────────────────────────

register_user_handlers(bot, download_queue_manager)
register_admin_handlers(bot)
register_login_handlers(bot)

from spideybot.core.handlers.outgoing import set_download_manager
set_download_manager(download_queue_manager)
logger.info("All handlers registered")

# ─── Graceful Shutdown ──────────────────────────────────────────────

_shutdown_event = asyncio.Event()


async def _handle_shutdown(signal_name: str) -> None:
    """Drain workers, close sessions, and disconnect the bot."""
    if _shutdown_event.is_set():
        return  # already shutting down
    _shutdown_event.set()
    log = logger.bind(signal=signal_name)
    log.info("Shutdown signal received — stopping gracefully")

    # 1. Stop accepting new tasks
    download_queue_manager.running = False

    # 2. Cancel all queued tasks so workers unblock
    async with download_queue_manager.user_queues_lock:
        for task in list(download_queue_manager.active_tasks.values()):
            if not task.is_cancelled:
                task.cancel()

    # 3. Stop workers (waits for in-flight tasks to finish)
    await download_queue_manager.stop_workers()
    log.info("Download workers stopped")

    # 4. Close TeraBox aiohttp session if open
    if terabox_downloader is not None:
        try:
            await terabox_downloader.close()
        except Exception:
            pass
        log.info("TeraBox session closed")

    # 5. Disconnect all user account clients
    await user_sessions.stop_all_clients()
    log.info("User account clients stopped")

    # 6. Disconnect Telegram client
    if bot.is_connected():
        await bot.disconnect()
        log.info("Telegram client disconnected")


def main():
    """Entry point for the bot."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Install signal handlers (SIGINT / SIGTERM) on the running loop.
    # loop.add_signal_handler() is Unix-only; fall back to signal.signal()
    # on Windows which still runs in the event-loop thread.
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.ensure_future(_handle_shutdown(s.name)),
            )
    else:
        def _win_signal_handler(sig_num, _frame):
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(_handle_shutdown(signal.Signals(sig_num).name))
            )
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, _win_signal_handler)

    logger.info("Starting Telegram bot")

    async def _run():
        db.init_db()
        # Clear leftover downloads from previous runs
        shutil.rmtree("./downloads", ignore_errors=True)
        download_queue_manager.start_workers()

        await bot.start(bot_token=config.TG_BOT_TOKEN)  # type: ignore[union-attr]  # Telethon stubs incomplete
        logger.info("Bot is running and listening for messages")

        # Block until disconnect or shutdown
        await bot.run_until_disconnected()
        # If we got here without a signal, still clean up
        if not _shutdown_event.is_set():
            await _handle_shutdown("clean-exit")

    try:
        loop.run_until_complete(_run())
    except Exception as e:
        logger.exception("Fatal error in bot", error=str(e))
    finally:
        loop.close()
        logger.info("Event loop closed — goodbye")