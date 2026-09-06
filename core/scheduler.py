"""Background scheduler — periodic maintenance jobs.

A single asyncio task loops forever, running four cheap maintenance passes
at independent intervals:

- **reclaim** — re-queue jobs whose worker lease expired (crash recovery)
- **retention** — purge old ``download_history`` rows (downloads_retention_days)
- **quota sweep** — drop stale ``quota_usage`` counters older than 30 days
- **rate-limit sweep** — clear stale rate-limiter tokens

All work runs inside a short-lived ``session_scope`` so a single failure
doesn't take down the loop.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import delete

from core.config import get_settings
from core.db import session_scope
from core.models import DownloadHistory, QuotaUsage
from core.queue import reclaim_expired
from core.ratelimit import get_limiter

logger = logging.getLogger("spideybot.scheduler")

# ── Intervals (seconds) ─────────────────────────────────────────

RECLAIM_INTERVAL = 10
RETENTION_INTERVAL = 3600
QUOTA_SWEEP_INTERVAL = 3600
RATELIMIT_SWEEP_INTERVAL = 300


async def _run_reclaim() -> None:
    async with session_scope() as session:
        reclaimed = await reclaim_expired(session)
        if reclaimed:
            logger.info("reclaimed expired leases", count=reclaimed)


async def _run_retention() -> None:
    settings = get_settings()
    async with session_scope() as session:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.downloads_retention_days
        )
        result = await session.execute(
            delete(DownloadHistory).where(DownloadHistory.created_at < cutoff)
        )
        if result.rowcount:
            logger.info("purged history rows", count=result.rowcount)


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


async def _scheduler_loop() -> None:
    """Run maintenance passes forever, each on its own cadence."""
    last_reclaim = 0.0
    last_retention = 0.0
    last_quota = 0.0
    last_ratelimit = 0.0

    while True:
        now = asyncio.get_running_loop().time()

        if now - last_reclaim >= RECLAIM_INTERVAL:
            last_reclaim = now
            try:
                await _run_reclaim()
            except Exception:  # noqa: BLE001 — keep the loop alive
                logger.exception("reclaim pass failed")

        if now - last_retention >= RETENTION_INTERVAL:
            last_retention = now
            try:
                await _run_retention()
            except Exception:  # noqa: BLE001
                logger.exception("retention pass failed")

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

        await asyncio.sleep(1)


async def start_scheduler() -> asyncio.Task:
    """Start the background maintenance task and return it."""
    task = asyncio.create_task(_scheduler_loop(), name="spideybot-scheduler")
    logger.info("scheduler started")
    return task
