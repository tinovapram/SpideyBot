"""Per-user token-bucket rate limiter (in-memory, single-process).

Each user gets a bucket of *capacity* tokens, refilled at *refill_rate*
tokens/sec up to *capacity*.  ``allow()`` consumes one token or returns
``False`` when empty.

For multi-worker deployments, replace this with a Redis-backed limiter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

from core.config import get_settings


@dataclass
class _Bucket:
    tokens: float
    last_refill: float  # monotonic timestamp


class RateLimiter:
    """Thread-safe token-bucket limiter keyed by user id."""

    def __init__(self, capacity: int | None = None, refill_per_sec: float | None = None) -> None:
        settings = get_settings()
        self._capacity: float = float(capacity if capacity is not None else settings.rate_limit_per_minute)
        # Refill rate: capacity tokens per 60 seconds → tokens/sec
        self._refill_rate: float = (
            refill_per_sec
            if refill_per_sec is not None
            else self._capacity / 60.0
        )
        self._buckets: dict[int, _Bucket] = {}
        self._lock = Lock()

    def allow(self, user_id: int) -> bool:
        """Consume one token.  Returns ``True`` if allowed, ``False`` if rate-limited."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(user_id)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, last_refill=now)
                self._buckets[user_id] = bucket
            else:
                elapsed = now - bucket.last_refill
                if elapsed > 0:
                    bucket.tokens = min(
                        self._capacity, bucket.tokens + elapsed * self._refill_rate
                    )
                    bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def remaining(self, user_id: int) -> float:
        """Return current token count (non-blocking, approximate)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(user_id)
            if bucket is None:
                return self._capacity
            elapsed = now - bucket.last_refill
            if elapsed > 0:
                return min(self._capacity, bucket.tokens + elapsed * self._refill_rate)
            return bucket.tokens

    def reset(self, user_id: int) -> None:
        """Clear a user's bucket (admin override)."""
        with self._lock:
            self._buckets.pop(user_id, None)

    def clear_stale(self, max_age_seconds: int = 3600) -> int:
        """Remove buckets not touched in *max_age_seconds*. Returns count removed."""
        now = time.monotonic()
        removed = 0
        with self._lock:
            stale = [
                uid
                for uid, b in self._buckets.items()
                if now - b.last_refill > max_age_seconds
            ]
            for uid in stale:
                del self._buckets[uid]
                removed += 1
        return removed


# ── Module-level singleton ──────────────────────────────────────

_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    """Return the process-wide :class:`RateLimiter` singleton."""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
