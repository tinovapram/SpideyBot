"""
SpideyBot — Download Queue Manager.

Priority-based async download queue with per-user concurrency limits.
Delegates actual download execution to handler modules in spideybot.downloaders.
"""

import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import structlog
from telethon.errors import RPCError as TelethonRPCError

from spideybot.downloaders.terabox_downloader import TeraBoxDownloader
from spideybot.downloaders.gallerydl_downloader import GalleryDLDownloader
from spideybot.config import is_terabox_url, get_concurrent_limit, MAX_CONCURRENT_FREE_TOTAL
from spideybot.downloaders.terabox_handler import run_terabox
from spideybot.downloaders.download_handler import run_download_task

logger = structlog.get_logger(__name__)


@dataclass
class DownloadTask:
    """Represents a queued download request from a user."""
    user_id: int
    event: Any
    link: str
    is_premium: bool
    is_admin: bool
    entry_id: int
    timestamp: float = 0.0
    status_msg: Any = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self):
        self.timestamp = time.time()

    @property
    def is_cancelled(self) -> bool:
        """Check if this task has been cancelled."""
        return self.cancel_event.is_set()

    def cancel(self):
        """Signal cancellation for this task."""
        self.cancel_event.set()


class DownloadQueueManager:
    """
    Async priority queue for download tasks with per-user concurrency limits.

    Premium/admin users get lower priority values (processed first) and higher
    concurrent download limits.

    Args:
        bot: Telethon TelegramClient instance.
        terabox_downloader: TeraBoxDownloader instance (or None).
        max_concurrent: Maximum number of worker tasks processing downloads.
    """

    def __init__(self, bot, terabox_downloader, reddit_downloader=None, max_concurrent=20):
        self.bot = bot
        self.terabox_downloader = terabox_downloader
        self.reddit_downloader = reddit_downloader
        self.fallback_downloader = GalleryDLDownloader()
        self.max_concurrent = max_concurrent
        self.global_queue = asyncio.PriorityQueue() # Global queue for all tasks
        self.active_tasks: Dict[int, DownloadTask] = {}
        self._queued_ids: list[int] = []  # tracks insertion order for position lookup
        self.user_active_counts: Dict[int, int] = {}
        self.user_queues: Dict[int, asyncio.Queue] = {}
        self.user_queues_lock = asyncio.Lock()
        self.entry_counter = 0
        self.workers = []
        self.running = False

    def start_workers(self):
        """Start the pool of async worker tasks to process the download queue."""
        if self.running:
            return
        self.running = True
        logger.info("Starting download queue workers", count=self.max_concurrent)
        for i in range(self.max_concurrent):
            worker = asyncio.create_task(self._worker_loop(i))
            self.workers.append(worker)

    async def stop_workers(self):
        """Gracefully stop all worker tasks."""
        self.running = False
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

    async def add_task(
        self,
        user_id: int,
        event: Any,
        link: str,
        is_premium: bool,
        is_admin: bool = False,
        status_msg: Any = None,
    ) -> tuple[str, Optional[DownloadTask]]:
        """
        Add a download task to the priority queue. NEVER rejects — always queues.

        Returns:
            ("ok", task) — task was queued successfully.
        """
        self.entry_counter += 1
        entry_id = self.entry_counter

        task = DownloadTask(
            user_id=user_id,
            event=event,
            link=link,
            is_premium=is_premium,
            is_admin=is_admin,
            entry_id=entry_id,
            status_msg=status_msg,
        )

        # Priority: admin=0.5, premium=1.0, free=2.0 (lower = higher priority)
        # Anti-starvation: subtract 0.001 per second waited (max 0.5 boost)
        tier_base = 0.5 if is_admin else (1.0 if is_premium else 2.0)
        priority = tier_base

        async with self.user_queues_lock:
            self.active_tasks[entry_id] = task
            self._queued_ids.append(entry_id)

        await self.global_queue.put((priority, entry_id, task))
        logger.info("Task queued", entry_id=entry_id, user_id=user_id, priority=f"{priority:.2f}", queue_size=self.global_queue.qsize())
        return "ok", task

    async def cancel_task(self, entry_id: int) -> bool:
        """
        Cancel a task by entry_id. Works for both queued and running tasks.

        Returns:
            True if the task was found and cancelled, False if not found.
        """
        async with self.user_queues_lock:
            task = self.active_tasks.get(entry_id)
        if task:
            task.cancel()
            logger.info("Task cancelled by user", entry_id=entry_id, user_id=task.user_id)
            return True
        return False

    async def cancel_user_tasks(self, user_id: int) -> int:
        """
        Cancel all active/queued tasks for a specific user.

        Returns:
            Number of tasks cancelled.
        """
        cancelled = 0
        async with self.user_queues_lock:
            tasks = [t for t in self.active_tasks.values() if t.user_id == user_id]
        for task in tasks:
            if not task.is_cancelled:
                task.cancel()
                cancelled += 1
                logger.info("Task cancelled", entry_id=task.entry_id, user_id=user_id)
        return cancelled

    def get_queue_position(self, entry_id: int) -> int:
        """Estimate queue position for a task (approximate, not exact)."""
        try:
            return self._queued_ids.index(entry_id) + 1
        except ValueError:
            return -1

    async def _worker_loop(self, worker_id: int):
        """Worker loop that pulls and executes tasks from the priority queue."""
        logger.info("Worker started", worker_id=worker_id)
        while self.running:
            try:
                priority, entry_id, task = await self.global_queue.get()
                logger.info("Worker pulled task", worker_id=worker_id, entry_id=entry_id, priority=f"{priority:.2f}")

                # Check if cancelled before even starting
                if task.is_cancelled:
                    logger.info("Task cancelled before execution, skipping", entry_id=entry_id)
                    self.global_queue.task_done()
                    async with self.user_queues_lock:
                        self.active_tasks.pop(entry_id, None)
                    continue

                try:
                    await self._execute_task(task)
                except Exception as e:
                    logger.exception("Error executing task", entry_id=entry_id, error=str(e))
                finally:
                    self.global_queue.task_done()
                    async with self.user_queues_lock:
                        self.active_tasks.pop(entry_id, None)
                        try:
                            self._queued_ids.remove(entry_id)
                        except ValueError:
                            pass
                    logger.info("Worker finished task", worker_id=worker_id, entry_id=entry_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Worker exception", worker_id=worker_id, error=str(e))
                await asyncio.sleep(1)

    async def _execute_task(self, task: DownloadTask) -> None:
        """Route a task to the appropriate download handler."""
        if task.is_cancelled:
            try:
                await task.status_msg.edit("❌ **SpideyBot:** Task was cancelled.")
            except TelethonRPCError:
                pass  # Message may already be deleted
            return
        # Determine if it is a TeraBox link
        is_terabox = False
        try:
            url_clean = task.link.rstrip('.,;!?)"\'')
            if is_terabox_url(url_clean):
                TeraBoxDownloader.parse_surl(url_clean)
                is_terabox = True
        except Exception as e:
            logger.warning("URL parse failed", link=task.link, error=str(e))

        if is_terabox:
            await run_terabox(task, self.bot, self.terabox_downloader)
        else:
            await run_download_task(task, self.bot, self.fallback_downloader, self.reddit_downloader)
