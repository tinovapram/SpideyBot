"""In-memory task context + worker loop + manager surface.

Bridges the persistent :mod:`core.queue` (PostgreSQL-backed jobs) with the
flow modules (:mod:`downloader.flow`, :mod:`downloader.terabox_flow`) that
expect an in-memory task object carrying ``.tier``, ``.is_admin``, ``.event``,
``.status_msg``, ``.is_cancelled`` etc.

Also exposes the manager surface that V3 handlers already call:
``add_task``, ``cancel_task``, ``get_queue_position``, ``active_tasks``,
``user_tasks``, ``task_done``.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from core import queue as Q, sessions
from core.config import get_settings
from core.cookies import shared_cookies
from core.db import session_scope
from core.metrics import get_metrics
from core.models import User, get_or_create_user
from core.tiers import HEAVY_SITES, effective_tier, tier_policy

if TYPE_CHECKING:
    from telethon import TelegramClient, types

logger = structlog.get_logger(__name__)


# ── Minimal event adapter ──────────────────────────────────────────


class _MessageStub:
    """Minimal Telethon ``Message`` stand-in with ``.id`` and ``.edit()``."""

    __slots__ = ("id", "_client", "_chat_id", "_text")

    def __init__(self, msg_id: int, client: Any, chat_id: int) -> None:
        self.id = msg_id
        self._client = client
        self._chat_id = chat_id
        self._text = ""

    async def edit(self, text: str, **_kw: Any) -> None:
        self._text = text
        if self._client is not None:
            try:
                await self._client.edit_message(self._chat_id, self.id, text)
            except Exception:
                pass  # status updates are best-effort


class _TaskEvent:
    """Bare ``.event`` surface that flows call ``.reply()`` / ``.chat_id`` on."""

    __slots__ = ("chat_id", "message", "_client")

    def __init__(self, client: Any, chat_id: int, msg_id: int) -> None:
        self._client = client
        self.chat_id = chat_id
        self.message = _MessageStub(msg_id, client, chat_id)

    async def reply(self, text: str, **_kw: Any) -> _MessageStub:
        if self._client is not None:
            try:
                msg = await self._client.send_message(self.chat_id, text, reply_to=self.message.id)
                self.message.id = msg.id if msg else self.message.id
                return self.message
            except Exception:
                pass
        return self.message


# ── In-memory task ─────────────────────────────────────────────────


@dataclass
class DownloadTask:
    """Lightweight in-memory task passed to flow modules."""

    entry_id: int
    user_id: int
    link: str
    site: str
    tier: str
    is_admin: bool
    event: _TaskEvent
    status_msg: _MessageStub | None = None
    is_cancelled: bool = False
    job_id: int | None = None

    def cancel(self) -> None:
        self.is_cancelled = True


# ── Site detection ─────────────────────────────────────────────────


_TERABOX_HOSTS: frozenset[str] = frozenset(
    {
        "terabox.app",
        "www.terabox.app",
        "www.terabox.com",
        "dm.terabox.app",
        "www.1024tera.com",
        "www.1024terabox.com",
    }
)


def _is_terabox_host(url: str) -> bool:
    """Fast host-based check without *parse_surl* validation."""
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        return host in _TERABOX_HOSTS or host.endswith(".terabox.app") or host.endswith(".terabox.com")
    except Exception:
        return False


def detect_site(url: str) -> str | None:
    """Return the canonical site name for *url*, or ``None`` for gallery-dl."""
    # Fast host check first (no surl validation needed for routing)
    if _is_terabox_host(url):
        return "terabox"

    # Fallback: try parse_surl for non-host patterns (e.g. raw surl strings)
    try:
        from downloader.terabox import parse_surl

        parse_surl(url)
        return "terabox"
    except Exception:
        pass

    from downloader.registry import get_registry

    hit = get_registry().detect(url)
    return hit[0] if hit else None


# ── TeraBox pool builder ──────────────────────────────────────────


async def build_terabox_pool() -> Any:
    """Build a :class:`TeraBoxAccountPool` from bot config + shared cookies.

    Returns the pool, or ``None`` if no bot cookies are configured.
    Returns a single :class:`TeraBoxDownloader` when only one account exists.
    """
    from downloader.terabox import TeraBoxAccountPool, TeraBoxDownloader

    settings = get_settings()
    account_cookies = settings.terabox_account_cookies()
    if not account_cookies:
        return None

    downloaders = []
    for cookie_str in account_cookies:
        try:
            downloaders.append(TeraBoxDownloader(cookie=cookie_str))
        except Exception as exc:
            logger.warning("Failed to build TeraBox downloader", error=str(exc))

    if not downloaders:
        return None
    if len(downloaders) == 1:
        return downloaders[0]

    pool = TeraBoxAccountPool(downloaders)
    logger.info("TeraBox pool initialized", accounts=pool.size)
    return pool


async def _append_shared_cookies(pool: Any, session: Any) -> Any:
    """Append free-user cookies to the pool for this request."""
    from downloader.terabox import TeraBoxAccountPool, TeraBoxDownloader

    try:
        extra = await shared_cookies(session)
    except Exception:
        return pool

    if not extra:
        return pool

    existing = pool.accounts if isinstance(pool, TeraBoxAccountPool) else [pool]
    new_downloaders = list(existing)
    for cookie_str in extra:
        try:
            new_downloaders.append(TeraBoxDownloader(cookie=cookie_str))
        except Exception:
            pass

    if len(new_downloaders) == len(existing):
        return pool  # nothing added

    if isinstance(pool, TeraBoxAccountPool):
        pool.accounts = new_downloaders
        return pool
    return TeraBoxAccountPool(new_downloaders)


# ── DownloadManager ───────────────────────────────────────────────


class DownloadManager:
    """Manages the bridge between Telegram commands and the DB queue.

    Handlers call :meth:`add_task`, :meth:`cancel_task`, etc.
    The worker loop (started via :meth:`start`) claims and executes jobs.
    """

    def __init__(self, bot: Any, terabox_downloader: Any) -> None:
        self.bot = bot
        self._terabox_downloader = terabox_downloader
        self.active_tasks: dict[int, DownloadTask] = {}
        self._running = False
        self._worker_task: asyncio.Task | None = None

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
        site = detect_site(link)
        tier = "premium" if is_admin else ("pro" if is_premium else "free")

        async with session_scope() as session:
            user = await get_or_create_user(session, user_id)
            real_tier = effective_tier(user.tier, user.tier_expiry)
            if real_tier != tier:
                tier = real_tier
            job = await Q.enqueue(
                session,
                user_id=user_id,
                link=link,
                site=site,
                tier=tier,
                is_admin=is_admin,
            )
            await session.commit()
            job_id = job.id

        chat_id = event.chat_id if hasattr(event, "chat_id") else event.message.chat_id
        msg_id = status_msg.id if hasattr(status_msg, "id") else status_msg.message_id
        adapter = _TaskEvent(self.bot, chat_id, msg_id)

        task = DownloadTask(
            entry_id=job_id,
            user_id=user_id,
            link=link,
            site=site or "unknown",
            tier=tier,
            is_admin=is_admin,
            event=adapter,
            status_msg=_MessageStub(msg_id, self.bot, chat_id),
            job_id=job_id,
        )
        self.active_tasks[job_id] = task
        get_metrics().incr("tasks_enqueued")
        return "ok", task

    async def cancel_task(self, entry_id: int) -> bool:
        """Cancel a queued task. Returns True if found."""
        task = self.active_tasks.get(entry_id)
        if task is not None and not task.is_cancelled:
            task.cancel()
            async with session_scope() as session:
                await Q.cancel(session, entry_id)
                await session.commit()
            get_metrics().incr("tasks_cancelled")
            return True
        return False

    def get_queue_position(self, entry_id: int) -> int:
        """1-based queue position; 0 if not found."""
        if entry_id not in self.active_tasks:
            return 0
        # Order by job_id (insertion/priority order from DB).
        sorted_ids = sorted(self.active_tasks.keys())
        try:
            return sorted_ids.index(entry_id) + 1
        except ValueError:
            return 0

    def user_tasks(self, user_id: int) -> list[DownloadTask]:
        """All active tasks for *user_id*."""
        return [t for t in self.active_tasks.values() if t.user_id == user_id]

    def task_done(self, entry_id: int) -> None:
        """Remove a task from active tracking."""
        self.active_tasks.pop(entry_id, None)

    # ── Worker lifecycle ────────────────────────────────────────

    def start(self) -> None:
        """Start the background worker loop."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("DownloadManager worker started")

    async def stop(self) -> None:
        """Cancel the worker loop and close the TeraBox pool."""
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        if self._terabox_downloader is not None:
            try:
                await self._terabox_downloader.close()
            except Exception:
                pass
        logger.info("DownloadManager worker stopped")

    # ── Worker loop ─────────────────────────────────────────────

    async def _worker_loop(self) -> None:
        """Claim and execute jobs from the persistent queue."""
        logger.info("Worker loop starting")
        while self._running:
            try:
                async with session_scope() as session:
                    job = await Q.claim_next(session)
                    if job is None:
                        await session.commit()
                        await asyncio.sleep(1)
                        continue
                    await session.commit()
                await self._run_job(job)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Worker loop error")
                await asyncio.sleep(1)

    async def _run_job(self, job: Any) -> None:
        """Build a task and dispatch to the appropriate flow."""
        entry_id = job.id
        task = self.active_tasks.get(entry_id)
        if task is None:
            adapter = _TaskEvent(self.bot, 0, 0)
            task = DownloadTask(
                entry_id=entry_id,
                user_id=job.user_id,
                link=job.link,
                site=job.site or "unknown",
                tier=job.tier,
                is_admin=False,
                event=adapter,
                job_id=job.id,
            )
            self.active_tasks[entry_id] = task

        # ── Check cancellation before dispatch ──────────────────
        if task.is_cancelled:
            async with session_scope() as session:
                db_job = await Q.get_job(session, entry_id)
                if db_job is not None:
                    await Q.fail(session, db_job, retry=False)
                await session.commit()
            self.task_done(entry_id)
            return

        # ── User lookup + tier refresh ──────────────────────────
        try:
            async with session_scope() as session:
                user = await session.get(User, job.user_id)
                if user is not None:
                    task.tier = effective_tier(user.tier, user.tier_expiry)
                    task.is_admin = user.is_admin
        except Exception:
            pass  # fall back to job-tier

        # ── Site gating ─────────────────────────────────────────
        if task.site and task.site in HEAVY_SITES:
            # Heavy sites require a cookie — check bot pool or user cookie
            has_bot_pool = self._terabox_downloader is not None
            has_user_cookie = False
            try:
                from core.cookies import has_cookie

                async with session_scope() as session:
                    has_user_cookie = await has_cookie(session, job.user_id)
            except Exception:
                pass

            if not has_bot_pool and not has_user_cookie:
                if task.status_msg is not None:
                    try:
                        await task.status_msg.edit(
                            "⚠️ **SpideyBot:** TeraBox is not configured. "
                            "No bot cookie or user cookie available."
                        )
                    except Exception:
                        pass
                async with session_scope() as session:
                    db_job = await Q.get_job(session, entry_id)
                    if db_job is not None:
                        await Q.fail(session, db_job, retry=False)
                    await session.commit()
                self.task_done(entry_id)
                return
        else:
            # Standard site gating via tier policy
            policy = tier_policy(task.tier)
            if not policy.sites.allows(task.site or ""):
                if task.status_msg is not None:
                    try:
                        await task.status_msg.edit(
                            f"⚠️ **SpideyBot:** Your tier ({task.tier}) "
                            f"does not have access to {task.site or 'this site'}."
                        )
                    except Exception:
                        pass
                async with session_scope() as session:
                    db_job = await Q.get_job(session, entry_id)
                    if db_job is not None:
                        await Q.fail(session, db_job, retry=False)
                    await session.commit()
                self.task_done(entry_id)
                return

        # ── Get user client ─────────────────────────────────────
        client = sessions.get_client(job.user_id)
        if client is None:
            client = self.bot

        # ── Resolve TeraBox downloader (with shared cookies) ────
        terabox = self._terabox_downloader

        try:
            if task.site == "terabox":
                if terabox is not None:
                    async with session_scope() as session:
                        terabox = await _append_shared_cookies(terabox, session)
                from downloader.terabox_flow import run_terabox

                await run_terabox(task, client, terabox)
            else:
                from downloader.flow import run_download

                await run_download(task, client)
        except Exception:
            logger.exception(
                "Download failed",
                user_id=job.user_id,
                link=job.link,
                site=task.site,
            )
            try:
                async with session_scope() as session:
                    db_job = await Q.get_job(session, entry_id)
                    if db_job is not None:
                        retried = await Q.fail(session, db_job, retry=True)
                        if retried:
                            logger.info("Job re-queued for retry", job_id=entry_id)
                await session.commit()
            except Exception:
                logger.exception("Failed to record job failure", job_id=entry_id)
            self.task_done(entry_id)
            return

        # ── Success ─────────────────────────────────────────────
        try:
            async with session_scope() as session:
                db_job = await Q.get_job(session, entry_id)
                if db_job is not None:
                    await Q.complete(session, db_job)
                await session.commit()
        except Exception:
            logger.exception("Failed to record job completion", job_id=entry_id)
        self.task_done(entry_id)


# ── Module-level singleton ─────────────────────────────────────────

_manager: DownloadManager | None = None


def get_manager() -> DownloadManager | None:
    """Return the global :class:`DownloadManager`, or ``None``."""
    return _manager


def set_manager(mgr: DownloadManager) -> None:
    """Set the global :class:`DownloadManager`."""
    global _manager
    _manager = mgr
