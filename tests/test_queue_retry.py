from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from core import queue


class _Metrics:
    def __init__(self) -> None:
        self.failed = 0

    def incr(self, name: str) -> None:
        if name == "downloads_failed":
            self.failed += 1


@pytest.mark.asyncio
async def test_retry_running_job_counts_attempts_and_fails_at_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _Metrics()
    monkeypatch.setattr(
        queue,
        "get_settings",
        lambda: SimpleNamespace(job_max_attempts=3, job_lease_seconds=300),
    )
    monkeypatch.setattr(queue, "get_metrics", lambda: metrics)

    job = SimpleNamespace(
        attempts=1,
        status=queue.RUNNING,
        finished_at=None,
        claim_token="claim-token",
        lease_expires=None,
    )

    assert await queue.retry_running_job(None, job) is True
    assert job.attempts == 2
    assert job.status == queue.RUNNING
    assert job.finished_at is None
    assert job.lease_expires > datetime.now(UTC)

    job.attempts = 3
    assert await queue.retry_running_job(None, job) is False
    assert job.status == queue.FAILED
    assert job.finished_at is not None
    assert job.claim_token is None
    assert job.lease_expires is None
    assert metrics.failed == 2
