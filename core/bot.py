"""
Bot core: initialization, service wiring, handler registration and lifecycle.
"""

from __future__ import annotations

import asyncio
import signal
import shutil
import sys

import structlog
from telethon import TelegramClient

from core import config, db, sessions
from core.logging import setup_logging
from core.queue import DownloadQueueManager
from downloader.terabox import TeraBoxAccountPool, TeraBoxDownloader
from handler.admin import register_admin_handlers
from handler.login import register_login_handlers
from handler.outgoing import set_download_manager
from handler.user import register_user_handlers
from utils import paths

setup_logging()
logger = structlog.get_logger(__name__)

try:
    _api_id = config.validate_telegram_config()
except config.ConfigError as exc:
    logger.error(str(exc))
    raise SystemExit(1) from exc

bot = TelegramClient(
    str(paths.PROJECT_ROOT / "bot_session"),
    _api_id,
    config.TG_API_HASH,
)

terabox_downloader = None
_account_cookies = config.terabox_account_cookies()
if _account_cookies:
    try:
        downloaders = [
            TeraBoxDownloader(
                cookie=cookie,
                js_token=config.TERABOX_JSTOKEN,
                bds_token=config.TERABOX_BDSTOKEN,
            )
            for cookie in _account_cookies
            if cookie
        ]
        if len(downloaders) == 1:
            terabox_downloader = downloaders[0]
            logger.info("TeraBox downloader initialized")
        elif downloaders:
            terabox_downloader = TeraBoxAccountPool(downloaders)
            logger.info("TeraBox pool initialized", accounts=len(downloaders))
        else:
            logger.warning("TERABOX_COOKIE(s) present but empty — TeraBox features unavailable")
    except Exception as exc:
        logger.error("Failed to initialize TeraBox downloader", error=str(exc))
else:
    logger.warning("TERABOX_COOKIE not set — TeraBox features unavailable")

download_queue_manager = DownloadQueueManager(
    bot, terabox_downloader, max_concurrent=config.MAX_CONCURRENT_DOWNLOADS
)

register_user_handlers(bot, download_queue_manager)
register_admin_handlers(bot)
register_login_handlers(bot)
set_download_manager(download_queue_manager)
logger.info("All handlers registered")

_shutdown_event = asyncio.Event()


async def _handle_shutdown(signal_name: str) -> None:
    if _shutdown_event.is_set():
        return
    _shutdown_event.set()
    log = logger.bind(signal=signal_name)
    log.info("Shutdown signal received — stopping gracefully")

    download_queue_manager.running = False

    for task in list(download_queue_manager.active_tasks.values()):
        if not task.is_cancelled:
            task.cancel()

    await download_queue_manager.stop_workers()
    log.info("Download workers stopped")

    if terabox_downloader is not None:
        try:
            await terabox_downloader.close()
        except Exception:
            pass
        log.info("TeraBox session closed")

    await sessions.stop_all_clients()
    log.info("User account clients stopped")

    if bot.is_connected():
        await bot.disconnect()
        log.info("Telegram client disconnected")


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(_handle_shutdown(s.name)))
    else:
        def _win_handler(sig_num, _frame):
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(_handle_shutdown(signal.Signals(sig_num).name))
            )

        signal.signal(signal.SIGINT, _win_handler)
        signal.signal(signal.SIGTERM, _win_handler)

    async def run() -> None:
        paths.ensure_directories()
        db.init_db()
        shutil.rmtree(paths.DOWNLOADS_DIR, ignore_errors=True)
        paths.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

        download_queue_manager.start_workers()
        await bot.start(bot_token=config.TG_BOT_TOKEN)
        logger.info("Bot is running and listening for messages")
        await bot.run_until_disconnected()

        if not _shutdown_event.is_set():
            await _handle_shutdown("clean-exit")

    try:
        loop.run_until_complete(run())
    except Exception as exc:
        logger.exception("Fatal error in bot", error=str(exc))
    finally:
        loop.close()
        logger.info("Event loop closed — goodbye")
