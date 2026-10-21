"""Background scheduler — periodic maintenance jobs.

A single asyncio task loops forever, running four cheap maintenance passes
at independent intervals:

- **quota sweep** — drop stale ``quota_usage`` counters older than 30 days
- **rate-limit sweep** — clear stale rate-limiter tokens
- **session sweep** — disconnect idle user clients
- **send-gate sweep** — drop gates of disconnected clients and idle per-chat buckets

All work runs inside a short-lived ``session_scope`` so a single failure
doesn't take down the loop.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import delete

from core.db import session_scope
from core.models import QuotaUsage
from core.ratelimit import get_limiter

logger = structlog.get_logger("spideybot.scheduler")

# ── Intervals (seconds) ─────────────────────────────────────────

QUOTA_SWEEP_INTERVAL = 3600
RATELIMIT_SWEEP_INTERVAL = 300
SESSION_SWEEP_INTERVAL = 60
SENDGATE_SWEEP_INTERVAL = 300


async def _run_quota_sweep() -> None:
    async with session_scope() as session:
        from datetime import date, timedelta

        cutoff = date.today() - timedelta(days=30)
        result = await session.execute(
            delete(QuotaUsage).where(QuotaUsage.period_start < cutoff)
        )
        if result.rowcount:
            logger.info("purged stale quota counters", count=result.rowcount)


def _run_ratelimit_sweep() -> None:
    get_limiter().clear_stale()


def _run_sendgate_sweep() -> None:
    from core import sendgate

    sendgate.sweep()


async def _scheduler_loop() -> None:
    """Run maintenance passes forever, each on its own cadence."""
    last_quota = 0.0
    last_ratelimit = 0.0
    last_sessions = 0.0
    last_sendgate = 0.0

    while True:
        now = asyncio.get_running_loop().time()

        if now - last_quota >= QUOTA_SWEEP_INTERVAL:
            last_quota = now
            try:
                await _run_quota_sweep()
            except Exception:  # noqa: BLE001
                logger.exception("quota sweep failed")

        if now - last_ratelimit >= RATELIMIT_SWEEP_INTERVAL:
            last_ratelimit = now
            try:
                _run_ratelimit_sweep()
            except Exception:  # noqa: BLE001
                logger.exception("ratelimit sweep failed")

        if now - last_sessions >= SESSION_SWEEP_INTERVAL:
            last_sessions = now
            try:
                from core import sessions

                await sessions.evict_idle_clients()
            except Exception:  # noqa: BLE001
                logger.exception("session sweep failed")

        if now - last_sendgate >= SENDGATE_SWEEP_INTERVAL:
            last_sendgate = now
            try:
                _run_sendgate_sweep()
            except Exception:  # noqa: BLE001
                logger.exception("sendgate sweep failed")

        await asyncio.sleep(1)


async def start_scheduler() -> asyncio.Task:
    """Start the background maintenance task and return it."""
    task = asyncio.create_task(_scheduler_loop(), name="spideybot-scheduler")
    logger.info("scheduler started")
    return task
