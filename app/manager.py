"""In-memory task context + worker loop + manager surface.

Bridges the persistent :mod:`app.queue` (PostgreSQL-backed jobs) with the flow modules.
Exposes the manager surface that handlers call:
``add_task``, ``cancel_task``, ``get_queue_position``, ``active_tasks``,
``user_tasks``, ``task_done``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app import db as db_module
from app import queue as Q
from app.config import Settings, get_settings
from app.models import Job
from app.paths import job_tmp_dir
from app.tiers import resolve as resolve_tier, get_limits
from downloader.flow import download_link
from downloader.registry import chain_for, delegate_for
from telegram.sessions import get_pool

logger = logging.getLogger(__name__)


@dataclass
class DownloadTask:
    """In-memory representation of a queued download for handler-side tracking."""

    job_id: int
    user_id: int
    link: str
    tier: str
    is_admin: bool
    event: Any
    status_msg: Any
    is_cancelled: bool = False

    def cancel(self) -> None:
        self.is_cancelled = True


class DownloadManager:
    """Manages the bridge between Telegram commands and the DB queue.

    Handlers call :meth:`add_task`, :meth:`cancel_task`, etc.
    The worker loop (started via :meth:`start`) claims and executes jobs.
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.active_tasks: dict[int, DownloadTask] = {}
        self._running = False
        self._worker_tasks: list[asyncio.Task] = []

    # ── Handler-facing API ──────────────────────────────────────

    async def add_task(
        self,
        user_id: int,
        event: Any,
        link: str,
        is_premium: bool,
        is_admin: bool,
        status_msg: Any,
    ) -> tuple[str, DownloadTask]:
        """Enqueue a download and return ``(status, task)``."""
        from app.db import session as db_session

        tier = "premium" if is_admin else ("pro" if is_premium else "free")

        async with db_session() as session:
            job = await Q.enqueue(
                session,
                user_id=user_id,
                link=link,
                tier=tier,
            )
            job_id = job.id

        chat_id = getattr(event, "chat_id", None) if event is not None else None
        client = getattr(event, "client", None) if event is not None else None
        msg_id = getattr(status_msg, "id", None) if status_msg is not None else None
        event_adapter = _StatusStub(msg_id, client, chat_id)

        task = DownloadTask(
            job_id=job_id,
            user_id=user_id,
            link=link,
            tier=tier,
            is_admin=is_admin,
            event=event_adapter,
            status_msg=status_msg,
        )
        self.active_tasks[job_id] = task
        return "ok", task

    async def cancel_task(self, entry_id: int) -> bool:
        """Cancel a queued task. Returns True if found."""
        task = self.active_tasks.get(entry_id)
        if task is not None and not task.is_cancelled:
            task.cancel()
            async with db_module.session() as session:
                await Q.request_cancel(session, job_id=entry_id, user_id=task.user_id)
                await session.commit()
            return True
        return False

    def get_queue_position(self, entry_id: int) -> int:
        """1-based queue position; 0 if not found."""
        if entry_id not in self.active_tasks:
            return 0
        sorted_ids = sorted(self.active_tasks.keys())
        try:
            return sorted_ids.index(entry_id) + 1
        except ValueError:
            return 0

    def user_tasks(self, user_id: int) -> list[DownloadTask]:
        """All active tasks for *user_id*."""
        return [t for t in self.active_tasks.values() if t.user_id == user_id]

    def get_task(self, entry_id: int) -> DownloadTask | None:
        """Return the active task for *entry_id*, or ``None`` when unknown."""
        return self.active_tasks.get(entry_id)

    def task_done(self, entry_id: int) -> None:
        """Remove a task from active tracking."""
        self.active_tasks.pop(entry_id, None)

    # ── Worker lifecycle ────────────────────────────────────────

    def start(self) -> None:
        """Start the background worker pool."""
        if self._running:
            return
        self._running = True

    async def stop(self) -> None:
        """Stop all worker tasks."""
        self._running = False
        for t in self._worker_tasks:
            t.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

    # ── Job dispatch ────────────────────────────────────────────

    async def _run_job(self, job: Job) -> None:
        """Build a task and dispatch to the appropriate flow."""
        entry_id = job.id
        task = self.active_tasks.get(entry_id)
        if task is None:
            logger.warning("Orphan job — no active task, failing", job_id=entry_id)
            try:
                async with db_module.session() as session:
                    db_job = await session.get(Job, entry_id)
                    if db_job is not None:
                        await Q.mark_failed(session, db_job)
                        await session.commit()
            except Exception:
                logger.exception("Failed to record orphan job failure", job_id=entry_id)
            return

        if task.is_cancelled:
            async with db_module.session() as session:
                db_job = await session.get(Job, entry_id)
                if db_job is not None:
                    await Q.mark_failed(session, db_job)
                    await session.commit()
            self.task_done(entry_id)
            return

        # ── Refresh tier from DB ────────────────────────────────
        try:
            from app.models import User

            async with db_module.session() as session:
                user = await session.get(User, job.user_id)
                if user is not None:
                    eff = resolve_tier(
                        tier=user.tier,
                        tier_expiry=user.tier_expiry,
                        is_admin=user.is_admin,
                    )
                    task.tier = eff.name
                    task.is_admin = user.is_admin
        except Exception:
            pass  # fall back to job-tier

        # ── Get user client ─────────────────────────────────────
        pool = get_pool()
        if pool is not None:
            async with pool.borrow(job.user_id) as client:
                await self._dispatch(job, task, entry_id, client)
                return
        # Fall back to bot client if pool not configured
        await self._dispatch(job, task, entry_id, self.bot)

    async def _dispatch(
        self, job: Job, task: DownloadTask, entry_id: int, client: Any
    ) -> None:
        """Run the download through the flow pipeline."""
        settings = get_settings()
        limits = get_limits(settings, job.tier)
        try:
            await download_link(
                job.link,
                job_tmp_dir(job.id),
                limit_mb=limits.link_total_mb,
                chain=chain_for(job.link),
                delegate=delegate_for(settings),
                should_stop=lambda: task.is_cancelled,
                session=client,
            )
        except Exception:
            logger.exception(
                "Download failed",
                user_id=job.user_id,
                link=job.link,
            )
            try:
                async with db_module.session() as session:
                    db_job = await session.get(Job, entry_id)
                    if db_job is not None:
                        await Q.mark_failed(session, db_job)
                        await session.commit()
            except Exception:
                logger.exception("Failed to record download failure", job_id=entry_id)
            self.task_done(entry_id)
            return

        # Mark delivered if we got here without exception
        try:
            async with db_module.session() as session:
                db_job = await session.get(Job, entry_id)
                if db_job is not None:
                    await Q.mark_delivered(session, db_job)
                    await session.commit()
        except Exception:
            logger.exception("Failed to record download delivery", job_id=entry_id)
        self.task_done(entry_id)


class _StatusStub:
    """Minimal stand-in with ``.edit()`` for status updates."""

    __slots__ = ("id", "_client", "_chat_id", "_text")

    def __init__(self, msg_id: int | None, client: Any, chat_id: int | None) -> None:
        self.id = msg_id
        self._client = client
        self._chat_id = chat_id
        self._text = ""

    async def edit(self, text: str, **_kw: Any) -> None:
        self._text = text
        if self._client is not None and self._chat_id is not None and self.id is not None:
            try:
                await self._client.edit_message_text(self._chat_id, self.id, text)
            except Exception:
                pass  # status updates are best-effort
