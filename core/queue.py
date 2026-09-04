"""
Priority download queue with per-user concurrency limits and aging.

- A single bot-wide heap orders tasks by ``effective_priority`` (lower runs
  first): admin < premium < free.
- Aging prevents starvation: waiting tasks gain priority up to a cap.
- Per-user concurrency is enforced: a task only runs when its user is below
  their tier limit.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from telethon.errors import RPCError as TelethonRPCError

from core import config
from downloader.flow import run_download
from downloader.terabox_flow import run_terabox

logger = structlog.get_logger(__name__)

PRIORITY_ADMIN = 0.5
PRIORITY_PREMIUM = 1.0
PRIORITY_FREE = 2.0
BOOST_PER_SEC = 0.001
MAX_BOOST = 0.5

_POLL_INTERVAL = 0.2


@dataclass
class DownloadTask:
    """A queued download request."""

    user_id: int
    event: Any
    link: str
    is_premium: bool
    is_admin: bool
    entry_id: int
    status_msg: Any = None
    enqueued_at: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def base_priority(self) -> float:
        if self.is_admin:
            return PRIORITY_ADMIN
        return PRIORITY_PREMIUM if self.is_premium else PRIORITY_FREE

    def effective_priority(self, now: float) -> float:
        waited = max(0.0, now - self.enqueued_at)
        return self.base_priority - min(MAX_BOOST, waited * BOOST_PER_SEC)


class DownloadQueueManager:
    """Async priority queue with per-user concurrency enforcement."""

    def __init__(self, bot, terabox_downloader, max_concurrent: int = 20) -> None:
        self.bot = bot
        self.terabox_downloader = terabox_downloader
        self.max_concurrent = max_concurrent

        self._heap: list[tuple[float, int, DownloadTask]] = []
        self._seq = itertools.count()
        self._cond = asyncio.Condition()

        self.active_tasks: dict[int, DownloadTask] = {}
        self._running: set[int] = set()
        self._user_active: dict[int, int] = {}

        self.workers: list[asyncio.Task] = []
        self.running = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def start_workers(self) -> None:
        if self.running:
            return
        self.running = True
        logger.info("Starting download queue workers", count=self.max_concurrent)
        for index in range(self.max_concurrent):
            self.workers.append(asyncio.create_task(self._worker_loop(index)))

    async def stop_workers(self) -> None:
        self.running = False
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

    # ── Queue API ──────────────────────────────────────────────────

    async def add_task(
        self,
        user_id: int,
        event: Any,
        link: str,
        is_premium: bool,
        is_admin: bool = False,
        status_msg: Any = None,
    ) -> tuple[str, DownloadTask | None]:
        entry_id = next(self._seq)
        task = DownloadTask(
            user_id=user_id,
            event=event,
            link=link,
            is_premium=is_premium,
            is_admin=is_admin,
            entry_id=entry_id,
            status_msg=status_msg,
        )

        async with self._cond:
            self.active_tasks[entry_id] = task
            heapq.heappush(
                self._heap,
                (task.effective_priority(time.monotonic()), entry_id, task),
            )
            self._cond.notify()

        logger.info("Task queued", entry_id=entry_id, user_id=user_id, base_priority=task.base_priority)
        return "ok", task

    async def get_next(self) -> DownloadTask | None:
        """Return the highest-priority runnable task, or None when none is ready."""
        async with self._cond:
            live: list[tuple[float, int, DownloadTask]] = []
            for item in self._heap:
                if item[2].is_cancelled:
                    self.active_tasks.pop(item[1], None)
                else:
                    live.append(item)
            self._heap = live

            if not self._heap:
                return None

            now = time.monotonic()
            self._heap = [
                (task.effective_priority(now), entry_id, task)
                for _, entry_id, task in self._heap
            ]
            heapq.heapify(self._heap)

            for index, (_, entry_id, task) in enumerate(self._heap):
                if self._user_has_slot(task):
                    self._heap.pop(index)
                    heapq.heapify(self._heap)
                    self._user_active[task.user_id] = self._user_active.get(task.user_id, 0) + 1
                    self._running.add(entry_id)
                    return task

            return None

    async def task_done(self, entry_id: int) -> None:
        async with self._cond:
            task = self.active_tasks.pop(entry_id, None)
            self._running.discard(entry_id)
            if task is not None:
                remaining = self._user_active.get(task.user_id, 1) - 1
                if remaining <= 0:
                    self._user_active.pop(task.user_id, None)
                else:
                    self._user_active[task.user_id] = remaining
            self._cond.notify()

    # ── Cancellation / queries ─────────────────────────────────────

    async def cancel_task(self, entry_id: int) -> bool:
        task = self.active_tasks.get(entry_id)
        if task is None:
            return False
        task.cancel()
        logger.info("Task cancelled", entry_id=entry_id, user_id=task.user_id)
        return True

    async def cancel_user_tasks(self, user_id: int) -> int:
        targets = [t for t in self.active_tasks.values() if t.user_id == user_id]
        cancelled = 0
        for task in targets:
            if not task.is_cancelled:
                task.cancel()
                cancelled += 1
        return cancelled

    def get_queue_position(self, entry_id: int) -> int:
        """Return 1-based queue position for a queued task, or -1."""
        if entry_id in self._running:
            return 0
        if entry_id not in self.active_tasks:
            return -1
        ordered = sorted(
            (item[1] for item in self._heap),
            key=lambda eid: self.active_tasks[eid].effective_priority(time.monotonic()),
        )
        try:
            return ordered.index(entry_id) + 1
        except ValueError:
            return -1

    def user_tasks(self, user_id: int) -> list[DownloadTask]:
        return [
            t for t in self.active_tasks.values()
            if t.user_id == user_id and not t.is_cancelled
        ]

    # ── Internals ──────────────────────────────────────────────────

    def _user_has_slot(self, task: DownloadTask) -> bool:
        limit = config.get_concurrent_limit(task.is_premium or task.is_admin)
        return self._user_active.get(task.user_id, 0) < limit

    async def _worker_loop(self, worker_id: int) -> None:
        logger.info("Worker started", worker_id=worker_id)
        while self.running:
            task = await self.get_next()
            if task is None:
                await asyncio.sleep(_POLL_INTERVAL)
                continue

            logger.info("Worker picked task", worker_id=worker_id, entry_id=task.entry_id)
            try:
                await self._execute_task(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error executing task", entry_id=task.entry_id)
            finally:
                await self.task_done(task.entry_id)

    async def _execute_task(self, task: DownloadTask) -> None:
        if task.is_cancelled:
            if task.status_msg is not None:
                try:
                    await task.status_msg.edit("❌ **SpideyBot:** Task was cancelled.")
                except TelethonRPCError:
                    pass
            return

        sender_client = getattr(task.event, "client", None) or self.bot

        if _is_terabox(task.link):
            await run_terabox(task, sender_client, self.terabox_downloader)
        else:
            await run_download(task, sender_client)


def _is_terabox(link: str) -> bool:
    if not config.is_terabox_url(link.rstrip('.,;!?)"\'')):
        return False
    try:
        from downloader.terabox import TeraBoxDownloader
        TeraBoxDownloader.parse_surl(link)
        return True
    except Exception:
        return False
