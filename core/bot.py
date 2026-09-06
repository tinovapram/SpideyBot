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
from core.config import get_settings, is_admin
from core.logging import setup_logging
from core.worker import DownloadManager
from handler.admin import register_admin_handlers
from handler.cookie import register_cookie_handlers
from handler.login import register_login_handlers
from handler.outgoing import set_download_manager
from handler.user import register_user_handlers
from utils import paths

setup_logging()
logger = structlog.get_logger(__name__)

try:
    _settings = get_settings()
    _api_id = _settings.validate_telegram()
except config.ConfigError as exc:
    logger.error(str(exc))
    raise SystemExit(1) from exc

bot = TelegramClient(
    str(paths.PROJECT_ROOT / "bot_session"),
    _api_id,
    _settings.tg_api_hash,
)

terabox_downloader = None
_account_cookies = _settings.terabox_account_cookies()
if _account_cookies:
    try:
        from downloader.terabox import TeraBoxAccountPool, TeraBoxDownloader

        downloaders = [
            TeraBoxDownloader(
                cookie=cookie,
                js_token=_settings.terabox_jstoken,
                bds_token=_settings.terabox_bdstoken,
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

download_manager = DownloadManager(bot, terabox_downloader)

register_user_handlers(bot, download_manager)
register_admin_handlers(bot)
register_cookie_handlers(bot)
register_login_handlers(bot)
set_download_manager(download_manager)
logger.info("All handlers registered")

_shutdown_event = asyncio.Event()


async def _handle_shutdown(signal_name: str) -> None:
    if _shutdown_event.is_set():
        return
    _shutdown_event.set()
    log = logger.bind(signal=signal_name)
    log.info("Shutdown signal received — stopping gracefully")

    await download_manager.stop()
    log.info("Download manager stopped")

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

        download_manager.start()
        await bot.start(bot_token=_settings.tg_bot_token)
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
