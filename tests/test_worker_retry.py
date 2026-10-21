from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core import worker


class _Session:
    async def get(self, *_args: Any) -> None:
        return None


class _SessionScope:
    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_failed_job_retries_in_current_worker_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from downloader import flow

    job = SimpleNamespace(
        id=7, user_id=42, link="https://example.com/video", site="example", attempts=1
    )
    manager = worker.DownloadManager(bot=object(), terabox_downloader=None)
    task = worker.DownloadTask(
        entry_id=job.id,
        user_id=job.user_id,
        link=job.link,
        site="example",
        tier="free",
        is_admin=False,
        event=worker._TaskEvent(None, chat_id=1, msg_id=2),
    )
    manager.active_tasks[job.id] = task

    monkeypatch.setattr(worker, "session_scope", _SessionScope)
    monkeypatch.setattr(worker.sessions, "get_client", lambda _user_id: None)
    # Keep the backoff out of the test's runtime.
    monkeypatch.setattr(worker, "_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(worker, "_RETRY_JITTER_SECONDS", 0.0)
    retry_count = 0

    async def retry_running_job(_job: Any) -> bool:
        nonlocal retry_count
        retry_count += 1
        return True

    monkeypatch.setattr(worker.Q, "retry_running_job", retry_running_job)

    attempts = 0

    async def run_download(*_args: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary download failure")

    monkeypatch.setattr(flow, "run_download", run_download)

    await manager._run_job(job)
    assert job.id not in manager.active_tasks
    assert attempts == 2
    assert retry_count == 1
