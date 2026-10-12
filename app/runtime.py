"""Running the bot: client, session pool, workers, scheduler (P3's wiring, P5's session half).

Until now the process was the client alone: handlers answered, and nothing consumed the queue. This
module is what makes a job actually run, and it exists as its own file rather than inside
:mod:`app.bot` because the *order* is the contract:

1. **the client is started first**, so a handler that replies immediately after a message does not
   race the dispatcher's own registration;
2. **the pool is installed before the workers start**, because a claim taken by a worker can reach a
   Telegram job at any moment and the pool is what that job asks for;
3. **the scheduler starts last**, since a lease sweep that runs before there are leases has nothing
   to do and a reap that runs before the pool exists would have nothing to close.

Shutdown is the mirror image, and it is ordered for a different reason: workers first (so no job is
mid-flight when its clients vanish), then the scheduler, then the session pool — a client closed
while a job still held it would surface as a download that failed for no visible reason.

`client.run()` is deliberately *not* used. It blocks its own event loop and gives no place to start
the pool, the workers or the reaper, which is precisely why the plan says the pool is wired here
rather than inside the bot client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from pyrogram import idle

from app import capabilities, config, jobs, paths, scheduler, workers
from app import logging as app_logging
from app.config import Settings
from telegram import clients, delivery, sessions

log = logging.getLogger(__name__)


def sender_for(bot: Any) -> delivery.SenderFor:
    """Which client sends a job's files.

    The **bot**, always — including for a job whose files came from the user's own session. The
    alternative looks tempting (send as the user, into their Saved Messages) and is wrong for every
    command the bot has: the delivered files must appear in the chat where the user typed the
    command and where the status message they are watching lives. A file uploaded by a different
    account cannot reply to that message or replace it.

    A callable rather than a value because that is the shape ``delivery`` takes, and because P6's
    magic word *does* deliver to Saved Messages through the user's own session — a different
    contract on the same seam, which is exactly why the seam exists.
    """

    def lookup(_chat_id: int) -> Any:
        return bot

    return lookup


async def serve(settings: Settings | None = None, *, pool_size: int | None = None) -> int:
    """Start everything, idle until interrupted, and shut down in order."""
    config.load_env_file()
    resolved = settings or config.get_settings()
    paths.ensure_runtime_dirs()
    redactor = app_logging.setup_logging(secrets=resolved.secret_values())
    log.info(
        "starting",
        extra={
            "client_profile": resolved.client.profile,
            "test_mode": resolved.test_mode,
            "redacted_values": redactor.count,
        },
    )
    _warn_about_capabilities(resolved)

    for directory in paths.insecure_secret_dirs():
        log.warning(
            "credential directory is readable beyond this account", extra={"path": directory}
        )
    if resolved.test_mode:
        log.warning("test mode: using Telegram's test servers and data/sessions/test")

    bot = clients.build_bot(resolved)
    pool = sessions.SessionPool(resolved)
    sessions.set_pool(pool)
    worker_pool: workers.WorkerPool | None = None
    housekeeper: scheduler.Scheduler | None = None

    try:
        # Starting the client first means a construction or connection failure happens before any
        # task machinery exists, so nothing has to be unwound beyond the client itself.
        await bot.start()
        handler = jobs.build_handler(resolved, sender_for=sender_for(bot), client=bot)
        worker_pool = workers.WorkerPool(
            resolved,
            handler,
            size=pool_size if pool_size is not None else resolved.runtime.max_concurrent_downloads,
        )
        housekeeper = scheduler.Scheduler(resolved, sessions_pool=pool)
        log.info(
            "bot started",
            extra={
                "workers": worker_pool.size,
                "session_cap": resolved.runtime.session_max_live,
                "test_mode": resolved.test_mode,
            },
        )
        await worker_pool.start()
        await housekeeper.start()
        # Orphans are swept once at startup rather than on the schedule: they can only come from a
        # run that died, so this is the only moment they are interesting (D21).
        removed = await scheduler.sweep_orphans()
        if removed:
            log.info("swept %d orphaned staging dir(s) at startup", len(removed))
        await idle()
    except KeyboardInterrupt:  # pragma: no cover - the operator stopping the process
        log.info("interrupted")
    finally:
        await _shutdown(worker_pool, housekeeper, pool, bot)

    return 0


async def _shutdown(
    worker_pool: workers.WorkerPool | None,
    housekeeper: scheduler.Scheduler | None,
    pool: sessions.SessionPool,
    bot: Any,
) -> None:
    """Stop in the order that leaves nothing mid-flight."""
    if worker_pool is not None:
        await worker_pool.stop()
    if housekeeper is not None:
        await housekeeper.stop()
    await pool.close_all()
    sessions.set_pool(None)
    with contextlib.suppress(Exception):
        await bot.stop()
    log.info("stopped")


def _warn_about_capabilities(settings: Settings) -> None:
    """Report a missing capability once, loudly, rather than letting a job fail inexplicably."""
    for capability in capabilities.probe():
        if capability.ok:
            continue
        if capability.required:
            log.error("required capability missing", extra={"name": capability.name})
        else:
            log.info("optional capability absent", extra={"name": capability.name})


def run(settings: Settings | None = None) -> int:
    """The synchronous entry point (``python main.py run``)."""
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:  # pragma: no cover - the operator stopping the process
        pass
    return 0
