"""
SpideyBot — Download Queue Manager.

Priority-based async download queue with per-user concurrency limits.
Delegates actual download execution to handler modules in spideybot.downloaders.
"""

import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from spideybot.downloaders.terabox_downloader import TeraBoxDownloader
from spideybot.downloaders.gallerydl_downloader import GalleryDLDownloader
from spideybot.config import is_terabox_url, get_concurrent_limit, MAX_CONCURRENT_FREE_TOTAL
from spideybot.downloaders.terabox_handler import run_terabox
from spideybot.downloaders.gallerydl_handler import run_gallerydl

logger = logging.getLogger(__name__)


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

    def __post_init__(self):
        self.timestamp = time.time()


class DownloadQueueManager:
    """
    Async priority queue for download tasks with per-user concurrency limits.

    Premium/admin users get lower priority values (processed first) and higher
    concurrent download limits.

    Args:
        bot: Telethon TelegramClient instance.
        tb_downloader: TeraBoxDownloader instance (or None).
        max_concurrent: Maximum number of worker tasks processing downloads.
    """

    def __init__(self, bot, tb_downloader, reddit_downloader=None, max_concurrent=20):
        self.bot = bot
        self.tb_downloader = tb_downloader
        self.reddit_downloader = reddit_downloader
        self.gallerydl_downloader = GalleryDLDownloader()
        self.max_concurrent = max_concurrent
        self.queue = asyncio.PriorityQueue()
        self.user_counts = {}
        self.active_free_tasks = 0
        self.user_counts_lock = asyncio.Lock()
        self.entry_counter = 0
        self.workers = []
        self.running = False

    def start_workers(self):
        """Start the pool of async worker tasks to process the download queue."""
        if self.running:
            return
        self.running = True
        logger.info(f"Starting {self.max_concurrent} download queue worker tasks...")
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
    ) -> str:
        """
        Add a download task to the priority queue.

        Enforces per-user concurrency limits and global Free tier concurrency limits
        before accepting the task.

        Args:
            user_id: Telegram user ID.
            event: The Telegram event that triggered the download.
            link: The URL to download.
            is_premium: Whether the user has premium status.
            is_admin: Whether the user is an admin.
            status_msg: Optional existing status message to edit.

        Returns:
            "ok" if added, "user_limit" if user limit reached, "global_limit" if global free limit reached.
        """
        limit = get_concurrent_limit(is_premium)
        async with self.user_counts_lock:
            current_count = self.user_counts.get(user_id, 0)
            if current_count >= limit:
                return "user_limit"
            
            # Check global Free tier concurrency limit (QoS safeguard)
            if not (is_premium or is_admin):
                if self.active_free_tasks >= MAX_CONCURRENT_FREE_TOTAL:
                    return "global_limit"
                self.active_free_tasks += 1

            self.user_counts[user_id] = current_count + 1

        self.entry_counter += 1
        
        # Calculate dynamic priority based on active count (Fair Share Scheduling)
        # premium/admin base = 1.0, free base = 2.0
        tier_base = 1.0 if (is_premium or is_admin) else 2.0
        priority = tier_base + (current_count * 0.1)

        task = DownloadTask(
            user_id=user_id,
            event=event,
            link=link,
            is_premium=is_premium,
            is_admin=is_admin,
            entry_id=self.entry_counter,
            status_msg=status_msg
        )

        await self.queue.put((priority, self.entry_counter, task))
        logger.info(f"Added task {self.entry_counter} to queue. Priority: {priority:.1f} (user: {user_id}, active: {current_count})")
        return "ok"

    async def _worker_loop(self, worker_id: int):
        """Worker loop that pulls and executes tasks from the priority queue."""
        logger.info(f"Worker {worker_id} started.")
        while self.running:
            try:
                priority, entry_id, task = await self.queue.get()
                logger.info(f"Worker {worker_id} pulled task {entry_id} (priority {priority})")
                try:
                    await self._execute_task(task)
                except Exception as e:
                    logger.exception(f"Error executing task {entry_id}: {e}")
                finally:
                    self.queue.task_done()
                    # Decrement user count and active free count
                    async with self.user_counts_lock:
                        self.user_counts[task.user_id] = max(0, self.user_counts.get(task.user_id, 1) - 1)
                        if not (task.is_premium or task.is_admin):
                            self.active_free_tasks = max(0, self.active_free_tasks - 1)
                    logger.info(f"Worker {worker_id} finished task {entry_id}.")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Worker {worker_id} exception: {e}")
                await asyncio.sleep(1)

    async def _execute_task(self, task: DownloadTask):
        """Route a task to the appropriate download handler."""
        # Determine if it is a TeraBox link
        is_terabox = False
        try:
            url_clean = task.link.rstrip('.,;!?)"\'')
            if is_terabox_url(url_clean):
                TeraBoxDownloader.parse_surl(url_clean)
                is_terabox = True
        except Exception:
            pass

        if is_terabox:
            await run_terabox(task, self.bot, self.tb_downloader)
        else:
            await run_gallerydl(task, self.bot, self.gallerydl_downloader, self.reddit_downloader)
