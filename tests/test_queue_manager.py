"""Tests for core.queue — priority ordering, concurrency limits, aging."""

import time
from unittest.mock import MagicMock

from core.queue import DownloadQueueManager, DownloadTask


class TestDownloadTask:
    def test_fields(self):
        task = DownloadTask(
            user_id=1, event=MagicMock(), link="https://example.com/x",
            is_premium=False, is_admin=False, entry_id=1,
        )
        assert task.link == "https://example.com/x"
        assert task.is_cancelled is False
        assert task.base_priority > 1.0

    def test_admin_priority_lower_than_free(self):
        admin = DownloadTask(user_id=1, event=MagicMock(), link="u", is_premium=False, is_admin=True, entry_id=1)
        free = DownloadTask(user_id=2, event=MagicMock(), link="u", is_premium=False, is_admin=False, entry_id=2)
        assert admin.base_priority < free.base_priority

    def test_cancel_sets_event(self):
        task = DownloadTask(user_id=1, event=MagicMock(), link="u", is_premium=False, is_admin=False, entry_id=1)
        task.cancel()
        assert task.is_cancelled is True

    def test_aging_reduces_priority(self):
        task = DownloadTask(user_id=1, event=MagicMock(), link="u", is_premium=False, is_admin=False, entry_id=1)
        now = time.monotonic()
        aged = task.effective_priority(now + 1000)
        assert aged <= task.base_priority


class TestQueueManager:
    def _manager(self):
        return DownloadQueueManager(bot=MagicMock(), terabox_downloader=MagicMock(), max_concurrent=4)

    async def test_add_task_returns_ok(self):
        mgr = self._manager()
        status, task = await mgr.add_task(1, MagicMock(), "https://x.com", False)
        assert status == "ok"
        assert isinstance(task, DownloadTask)

    async def test_entry_ids_increment(self):
        mgr = self._manager()
        _, t1 = await mgr.add_task(1, MagicMock(), "u1", False)
        _, t2 = await mgr.add_task(1, MagicMock(), "u2", False)
        assert t2.entry_id == t1.entry_id + 1

    async def test_get_next_prefers_admin(self):
        mgr = self._manager()
        await mgr.add_task(1, MagicMock(), "free", False, is_admin=False)
        _, admin = await mgr.add_task(2, MagicMock(), "admin", False, is_admin=True)
        first = await mgr.get_next()
        assert first.entry_id == admin.entry_id

    async def test_per_user_concurrency_limit(self):
        mgr = self._manager()
        await mgr.add_task(10, MagicMock(), "a", False)
        await mgr.add_task(10, MagicMock(), "b", False)

        first = await mgr.get_next()
        assert first is not None and first.user_id == 10

        # Second task from the same user must wait until the slot frees.
        assert await mgr.get_next() is None

        await mgr.task_done(first.entry_id)
        second = await mgr.get_next()
        assert second is not None and second.entry_id != first.entry_id

    async def test_cancelled_tasks_skipped(self):
        mgr = self._manager()
        _, task = await mgr.add_task(1, MagicMock(), "cancelme", False)
        await mgr.cancel_task(task.entry_id)
        assert await mgr.get_next() is None

    async def test_queue_position(self):
        mgr = self._manager()
        _, task = await mgr.add_task(1, MagicMock(), "u", False)
        assert mgr.get_queue_position(task.entry_id) == 1
