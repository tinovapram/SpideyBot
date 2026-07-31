"""
Tests for spideybot.queue_manager — DownloadTask, queue operations, cancellation.
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from spideybot.queue_manager import DownloadTask, DownloadQueueManager


class TestDownloadTask:
    """Tests for the DownloadTask dataclass."""

    def test_create_task(self):
        task = DownloadTask(
            user_id=123,
            event=MagicMock(),
            link="https://example.com/video.mp4",
            is_premium=False,
            is_admin=False,
            entry_id=1,
        )
        assert task.user_id == 123
        assert task.link == "https://example.com/video.mp4"
        assert task.is_premium is False
        assert task.is_admin is False
        assert task.entry_id == 1
        assert task.timestamp > 0

    def test_timestamp_auto_set(self):
        before = time.time()
        task = DownloadTask(
            user_id=1, event=MagicMock(), link="url",
            is_premium=False, is_admin=False, entry_id=1,
        )
        after = time.time()
        assert before <= task.timestamp <= after

    def test_cancel_event_default_not_cancelled(self):
        task = DownloadTask(
            user_id=1, event=MagicMock(), link="url",
            is_premium=False, is_admin=False, entry_id=1,
        )
        assert task.is_cancelled is False

    def test_cancel_sets_event(self):
        task = DownloadTask(
            user_id=1, event=MagicMock(), link="url",
            is_premium=False, is_admin=False, entry_id=1,
        )
        task.cancel()
        assert task.is_cancelled is True

    def test_cancel_event_is_asyncio_event(self):
        task = DownloadTask(
            user_id=1, event=MagicMock(), link="url",
            is_premium=False, is_admin=False, entry_id=1,
        )
        assert isinstance(task.cancel_event, asyncio.Event)


class TestQueueManagerAddTask:
    """Tests for DownloadQueueManager.add_task()."""

    def _make_manager(self):
        bot = MagicMock()
        terabox_dl = MagicMock()
        return DownloadQueueManager(bot, terabox_dl)

    async def test_add_task_returns_tuple(self):
        mgr = self._make_manager()
        result = await mgr.add_task(
            user_id=100,
            event=MagicMock(),
            link="https://example.com",
            is_premium=False,
        )
        assert isinstance(result, tuple)
        assert result[0] == "ok"
        assert isinstance(result[1], DownloadTask)

    async def test_add_task_increments_entry_id(self):
        mgr = self._make_manager()
        _, task1 = await mgr.add_task(
            user_id=100, event=MagicMock(), link="url1", is_premium=False,
        )
        _, task2 = await mgr.add_task(
            user_id=100, event=MagicMock(), link="url2", is_premium=False,
        )
        assert task2.entry_id == task1.entry_id + 1

    async def test_task_in_global_queue(self):
        mgr = self._make_manager()
        await mgr.add_task(
            user_id=100, event=MagicMock(), link="url", is_premium=False,
        )
        assert mgr.global_queue.qsize() == 1

    async def test_priority_admin_lower_than_free(self):
        """Admin tasks should have lower priority (higher precedence)."""
        mgr = self._make_manager()
        _, admin_task = await mgr.add_task(
            user_id=1, event=MagicMock(), link="admin_url",
            is_premium=False, is_admin=True,
        )
        _, free_task = await mgr.add_task(
            user_id=2, event=MagicMock(), link="free_url",
            is_premium=False, is_admin=False,
        )
        # Drain the priority queue to check ordering
        items = []
        while not mgr.global_queue.empty():
            items.append(await mgr.global_queue.get())
        # Queue tuples are (priority, entry_id, task)
        # Admin should come before free (lower priority number)
        admin_idx = next(i for i, (p, eid, _) in enumerate(items) if eid == admin_task.entry_id)
        free_idx = next(i for i, (p, eid, _) in enumerate(items) if eid == free_task.entry_id)
        assert admin_idx < free_idx


class TestCancelTask:
    """Tests for cancellation methods."""

    def _make_manager(self):
        bot = MagicMock()
        terabox_dl = MagicMock()
        return DownloadQueueManager(bot, terabox_dl)

    async def test_cancel_task_found(self):
        mgr = self._make_manager()
        _, task = await mgr.add_task(
            user_id=100, event=MagicMock(), link="url", is_premium=False,
        )
        result = await mgr.cancel_task(task.entry_id)
        assert result is True
        assert task.is_cancelled is True

    async def test_cancel_task_not_found(self):
        mgr = self._make_manager()
        result = await mgr.cancel_task(9999)
        assert result is False

    async def test_cancel_user_tasks(self):
        mgr = self._make_manager()
        await mgr.add_task(user_id=50, event=MagicMock(), link="url1", is_premium=False)
        _, task2 = await mgr.add_task(user_id=50, event=MagicMock(), link="url2", is_premium=False)
        await mgr.add_task(user_id=99, event=MagicMock(), link="url3", is_premium=False)

        cancelled = await mgr.cancel_user_tasks(50)
        assert cancelled == 2
        assert task2.is_cancelled is True


class TestGetQueuePosition:
    """Tests for get_queue_position()."""

    def _make_manager(self):
        bot = MagicMock()
        terabox_dl = MagicMock()
        return DownloadQueueManager(bot, terabox_dl)

    async def test_position_known_task(self):
        mgr = self._make_manager()
        _, task = await mgr.add_task(
            user_id=1, event=MagicMock(), link="url", is_premium=False,
        )
        pos = mgr.get_queue_position(task.entry_id)
        assert pos >= 1

    async def test_position_unknown_task(self):
        mgr = self._make_manager()
        pos = mgr.get_queue_position(9999)
        assert pos == -1
