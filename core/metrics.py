"""In-process metrics counters for /stats (single-process).

Thread-safe integer gauges.  For multi-worker deployments, replace with a
Redis-backed counter — but the bot is single-process by design (see
ARCHITECTURE.md §1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


@dataclass
class Metrics:
    """Monotonic-ish counters incremented across the process lifetime."""

    downloads_started: int = 0
    downloads_completed: int = 0
    downloads_failed: int = 0
    downloads_cancelled: int = 0
    bytes_downloaded: int = 0
    bytes_uploaded: int = 0
    queue_claims: int = 0
    queue_reclaims: int = 0
    rate_limited: int = 0

    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def incr(self, attr: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, attr, getattr(self, attr) + amount)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                k: v
                for k, v in self.__dict__.items()
                if not k.startswith("_")
            }


# ── Module-level singleton ──────────────────────────────────────

_metrics: Metrics | None = None


def get_metrics() -> Metrics:
    """Return the process-wide :class:`Metrics` singleton."""
    global _metrics
    if _metrics is None:
        _metrics = Metrics()
    return _metrics
