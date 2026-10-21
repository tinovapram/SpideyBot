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
    queue_depth: int = 0
    active_downloads: int = 0
    active_sessions: int = 0
    download_latency_seconds: float = 0.0
    download_retries: int = 0
    downloads_by_site: dict[str, int] = field(default_factory=dict)
    failures_by_site: dict[str, int] = field(default_factory=dict)
    rate_limited: int = 0
    tasks_enqueued: int = 0
    tasks_cancelled: int = 0

    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def incr(self, attr: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, attr, getattr(self, attr, 0) + amount)

    def set(self, attr: str, value: int | float) -> None:
        with self._lock:
            setattr(self, attr, value)

    def record_download(self, site: str | None, latency_seconds: float) -> None:
        with self._lock:
            key = site or "unknown"
            self.download_latency_seconds += latency_seconds
            self.downloads_by_site[key] = self.downloads_by_site.get(key, 0) + 1

    def record_failure(self, site: str | None) -> None:
        with self._lock:
            key = site or "unknown"
            self.failures_by_site[key] = self.failures_by_site.get(key, 0) + 1

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {
                k: v
                for k, v in self.__dict__.items()
                if not k.startswith("_") and not isinstance(v, dict)
            }

    def prometheus(self) -> str:
        """Render counters and gauges in Prometheus text format."""
        snapshot = self.snapshot()
        lines = [
            "# HELP spideybot_downloads_total Completed downloads.",
            "# TYPE spideybot_downloads_total counter",
            f"spideybot_downloads_total {snapshot['downloads_completed']}",
            "# HELP spideybot_downloads_failed_total Failed downloads.",
            "# TYPE spideybot_downloads_failed_total counter",
            f"spideybot_downloads_failed_total {snapshot['downloads_failed']}",
            "# HELP spideybot_bytes_downloaded_total Downloaded bytes.",
            "# TYPE spideybot_bytes_downloaded_total counter",
            f"spideybot_bytes_downloaded_total {snapshot['bytes_downloaded']}",
            "# HELP spideybot_queue_depth Current queued jobs.",
            "# TYPE spideybot_queue_depth gauge",
            f"spideybot_queue_depth {snapshot['queue_depth']}",
            "# HELP spideybot_active_sessions Current active user sessions.",
            "# TYPE spideybot_active_sessions gauge",
            f"spideybot_active_sessions {snapshot['active_sessions']}",
        ]
        with self._lock:
            for site, count in sorted(self.downloads_by_site.items()):
                lines.append(f'spideybot_downloads_by_site_total{{site="{site}"}} {count}')
            for site, count in sorted(self.failures_by_site.items()):
                lines.append(f'spideybot_failures_by_site_total{{site="{site}"}} {count}')
        return "\n".join(lines) + "\n"


# ── Module-level singleton ──────────────────────────────────────

_metrics: Metrics | None = None


def get_metrics() -> Metrics:
    """Return the process-wide :class:`Metrics` singleton."""
    global _metrics
    if _metrics is None:
        _metrics = Metrics()
    return _metrics
