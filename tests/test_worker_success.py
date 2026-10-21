"""Worker happy path: quota consumption, referral credit and completion."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core import worker


class _Session:
    async def get(self, *_args: Any) -> Any:
        return SimpleNamespace(id=42, tier="free", tier_expiry=None, is_admin=False)


class _SessionScope:
    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *_args: object) -> None:
        return None


async def test_successful_job_consumes_quota_and_credits_referral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from downloader import flow

    job = SimpleNamespace(
        id=11, user_id=42, link="https://example.com/video", site="example", attempts=1
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

    recorded: dict[str, Any] = {}

    async def run_download(target: Any, _client: Any) -> None:
        target.completed = True
        target.downloaded_bytes = 2048

    async def consume(_session: Any, _user: Any, *, bytes_downloaded: int = 0, reservation: bool = False) -> None:
        recorded["consumed"] = (bytes_downloaded, reservation)

    async def credit(_session: Any, user_id: int) -> bool:
        recorded["credited"] = user_id
        return True

    async def complete(_job: Any, *, bytes_downloaded: int | None = None, filename: str | None = None) -> None:
        recorded["completed"] = bytes_downloaded

    monkeypatch.setattr(worker, "session_scope", _SessionScope)
    monkeypatch.setattr(worker.sessions, "get_client", lambda _user_id: None)
    monkeypatch.setattr(worker, "consume", consume)
    monkeypatch.setattr(worker, "try_credit_on_download", credit)
    monkeypatch.setattr(worker.Q, "complete", complete)
    monkeypatch.setattr(flow, "run_download", run_download)

    await manager._run_job(job)

    assert recorded == {"consumed": (2048, False), "credited": 42, "completed": 2048}
    assert job.id not in manager.active_tasks
