"""
Bot core: initialization, service wiring, handler registration and lifecycle.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys

import structlog
from sqlalchemy import text
from telethon import TelegramClient

from core import config, db, sessions
from core.config import Settings, get_settings
from core.logging import setup_logging
from core.scheduler import start_scheduler
from core.observability import set_component_status, start_server, stop_server
from core.worker import DownloadManager
from handler.admin import register_admin_handlers
from handler.bypass import register_bypass_handler
from handler.handler import install
from handler.login import register_login_handlers
from handler.setting import register_setting_handlers
from utils import paths

setup_logging()
logger = structlog.get_logger(__name__)

_settings: Settings | None = None
bot: TelegramClient | None = None
terabox_downloader = None
download_manager: DownloadManager | None = None
_shutdown_event: asyncio.Event | None = None
_scheduler_task: asyncio.Task[None] | None = None


def initialize() -> None:
    """Build the application and register handlers once the process starts."""
    global _settings, bot, terabox_downloader, download_manager

    if download_manager is not None:
        return

    try:
        _settings = get_settings()
        api_id = _settings.validate_telegram()
        logger.info(
            "Configuration loaded",
            metrics_host=_settings.metrics_host,
            metrics_port=_settings.metrics_port,
            session_max_live=_settings.session_max_live,
            session_idle_seconds=_settings.session_idle_seconds,
            max_concurrent_downloads=_settings.max_concurrent_downloads,
        )
    except config.ConfigError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    client = TelegramClient(
        str(paths.PROJECT_ROOT / "bot_session"),
        api_id,
        _settings.tg_api_hash,
    )
    bot = client

    account_cookies = _settings.terabox_account_cookies()
    if account_cookies:
        try:
            from downloader.terabox import TeraBoxAccountPool, TeraBoxDownloader

            downloaders = [
                TeraBoxDownloader(
                    cookie=cookie,
                    js_token=_settings.terabox_jstoken,
                    bds_token=_settings.terabox_bdstoken,
                )
                for cookie in account_cookies
                if cookie
            ]
            if len(downloaders) == 1:
                terabox_downloader = downloaders[0]
                logger.info("TeraBox downloader initialized")
            elif downloaders:
                terabox_downloader = TeraBoxAccountPool(downloaders)
                logger.info("TeraBox pool initialized", accounts=len(downloaders))
            else:
                logger.warning("TERABOX_COOKIES present but empty — TeraBox features unavailable")
        except Exception as exc:
            logger.error("Failed to initialize TeraBox downloader", error=str(exc))
    else:
        logger.warning("TERABOX_COOKIES not set — TeraBox features unavailable")

    download_manager = DownloadManager(client, terabox_downloader)

    # `handler` owns the command handlers; `install` also makes it reachable
    # from core.sessions so user clients can attach their own handlers.
    handler = install(client, download_manager)
    handler.register_bot_handlers()
    register_admin_handlers(client)
    register_bypass_handler(client)
    register_setting_handlers(client)
    register_login_handlers(client)
    logger.info("All handlers registered")


async def _handle_shutdown(signal_name: str) -> None:
    global _scheduler_task

    if _shutdown_event is None or _shutdown_event.is_set():
        return
    _shutdown_event.set()
    paths.READY_FILE.unlink(missing_ok=True)
    for component in ("database", "telegram", "worker", "scheduler"):
        set_component_status(component, False)
    await stop_server()
    log = logger.bind(signal=signal_name)
    log.info("Shutdown signal received — stopping gracefully")

    if _scheduler_task is not None:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background scheduler failed during shutdown")
        _scheduler_task = None
        log.info("Background scheduler stopped")

    if download_manager is not None:
        await download_manager.stop()
        log.info("Download manager stopped")

    await sessions.stop_all_clients()
    log.info("User account clients stopped")

    if bot is not None and bot.is_connected():
        await bot.disconnect()
        log.info("Telegram client disconnected")

    await db.dispose()
    log.info("Database engine disposed")


def main() -> None:
    global _shutdown_event

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
        global _scheduler_task, _shutdown_event

        try:
            _shutdown_event = asyncio.Event()
            paths.READY_FILE.unlink(missing_ok=True)
            initialize()
            paths.ensure_directories()
            db.init_db()
            await start_server()
            try:
                async with db.engine().begin() as connection:
                    await connection.execute(text("SELECT 1"))
                set_component_status("database", True)
            except Exception:
                logger.exception("Database readiness check failed")
                raise
            _scheduler_task = await start_scheduler()
            set_component_status("scheduler", True)
            shutil.rmtree(paths.DOWNLOADS_DIR, ignore_errors=True)
            paths.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

            download_manager.start()
            set_component_status("worker", True)
            assert bot is not None
            assert _settings is not None
            await bot.start(bot_token=_settings.tg_bot_token)
            set_component_status("telegram", True)
            paths.READY_FILE.write_text(str(os.getpid()), encoding="ascii")
            logger.info("Bot is running and listening for messages")
            await bot.run_until_disconnected()
        finally:
            if not _shutdown_event.is_set():
                await _handle_shutdown("run-exit")

    try:
        loop.run_until_complete(run())
    except Exception as exc:
        logger.exception("Fatal error in bot", error=str(exc))
        raise SystemExit(1) from exc
    finally:
        loop.close()
        logger.info("Event loop closed — goodbye")
