"""Runtime watchdogs: event-loop lag and Telegram connection recovery.

Two failure modes make the bot look alive while it answers nothing:

* a **blocked event loop** — a synchronous call (hachoir parsing a video,
  ffmpeg, a blocking HTTP client) holds the loop, so no MTProto traffic moves
  and ``/readyz`` never replies;
* a **dropped connection** — Telethon gives up after its own retries, the
  process keeps running, and ``/readyz`` reports 503 forever.

These tasks turn both into log lines, and recover the connection (or restart
the process so the container does).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]

_REPORT_EVERY_SECONDS = 30.0


async def monitor_loop_lag(
    *,
    threshold: float,
    interval: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Log when the event loop is blocked longer than *threshold* seconds.

    A coroutine cannot observe its own block, so this samples sleep drift: a
    0.5 s sleep that returns 8 s late means something synchronous held the loop.
    """
    blocked_since: float | None = None
    last_report = 0.0

    while True:
        started = clock()
        await sleep(interval)
        now = clock()
        lag = now - started - interval

        if lag >= threshold:
            if blocked_since is None:
                blocked_since = started
                last_report = now
                logger.error("event loop blocked", lag_seconds=round(lag, 1))
            elif now - last_report >= _REPORT_EVERY_SECONDS:
                last_report = now
                logger.warning(
                    "event loop still blocked",
                    blocked_seconds=round(now - blocked_since, 1),
                )
        elif blocked_since is not None:
            logger.info(
                "event loop recovered",
                blocked_seconds=round(now - blocked_since, 1),
            )
            blocked_since = None


async def watch_connections(
    bot: Any,
    *,
    interval: int,
    grace: int,
    max_failures: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Reconnect the bot client; forget user clients whose transport died."""
    if bot is None:
        return

    healthy_since = clock()
    failures = 0

    while True:
        await sleep(interval)
        await _prune_user_clients(grace)

        if bot.is_connected():
            if failures:
                logger.info("bot connection restored", attempts=failures)
            failures = 0
            healthy_since = clock()
            continue

        if clock() - healthy_since < grace:
            continue

        failures += 1
        logger.warning("bot disconnected, reconnecting", attempt=failures)
        try:
            await asyncio.wait_for(bot.connect(), timeout=max(15, grace))
        except Exception as exc:  # noqa: BLE001 — any failure counts as an attempt
            logger.warning("bot reconnect failed", attempt=failures, error=str(exc))

        if failures >= max_failures:
            logger.error(
                "bot could not reconnect — exiting so the container restarts",
                attempts=failures,
            )
            os._exit(1)


async def _prune_user_clients(grace: int) -> int:
    from core import sessions

    try:
        return await sessions.drop_disconnected_clients(grace_seconds=grace)
    except Exception:  # noqa: BLE001 — the watchdog must not die
        logger.exception("Failed to prune disconnected user clients")
        return 0
