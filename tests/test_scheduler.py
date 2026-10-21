from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from core import scheduler


@pytest.mark.asyncio
async def test_start_scheduler_runs_maintenance_task(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_op() -> None:
        return None

    monkeypatch.setattr(scheduler, "_run_quota_sweep", no_op)
    monkeypatch.setattr(scheduler, "_run_ratelimit_sweep", lambda: None)

    task = await scheduler.start_scheduler()
    try:
        await asyncio.sleep(0.05)
        assert task.get_name() == "spideybot-scheduler"
        assert not task.done()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
