from __future__ import annotations

import time

import pytest

from core.ratelimit import RateLimiter


def test_token_bucket_allows_capacity_then_denies() -> None:
    limiter = RateLimiter(capacity=2, refill_per_sec=0.0)

    assert limiter.allow(1) is True
    assert limiter.allow(1) is True
    assert limiter.allow(1) is False
    # Buckets are per user.
    assert limiter.allow(2) is True


def test_tokens_refill_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = RateLimiter(capacity=1, refill_per_sec=1.0)
    now = time.monotonic()

    assert limiter.allow(1) is True
    assert limiter.allow(1) is False

    monkeypatch.setattr(time, "monotonic", lambda: now + 2.0)
    assert limiter.allow(1) is True


def test_reset_restores_a_full_bucket() -> None:
    limiter = RateLimiter(capacity=1, refill_per_sec=0.0)

    assert limiter.allow(1) is True
    limiter.reset(1)

    assert limiter.allow(1) is True
