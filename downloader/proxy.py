"""Lightweight proxy pool for downloaders that need IP rotation.

Reads ``PROXY_POOL`` from the environment — a comma-separated list of
proxies (``protocol://host:port`` or ``ip:port``).  Each downloader site
gets its own rotation index so one site's activity doesn't exhaust the
pool for another.

Usage from a downloader::

    from ..proxy import get_proxy

    proxy_url = get_proxy("doodstream")  # or None if pool is empty
"""

from __future__ import annotations

import os
import threading

_pool: list[str] = []
_lock = threading.Lock()
_counters: dict[str, int] = {}


def _load() -> list[str]:
    global _pool  # noqa: PLW0603
    with _lock:
        if _pool is not None and _pool:
            return _pool
        raw = os.getenv("PROXY_POOL", "").strip()
        if not raw:
            _pool = []
        else:
            _pool = [p.strip() for p in raw.split(",") if p.strip()]
        return _pool


def get_proxy(site: str = "default") -> str | None:
    """Return the next proxy URL for *site*, or ``None`` if no pool is set.

    Round-robin across the pool per site.  No validation — if a proxy is
    dead, ``requests`` will raise and the caller's retry logic handles it.
    """
    pool = _load()
    if not pool:
        return None
    with _lock:
        idx = _counters.get(site, 0)
        _counters[site] = (idx + 1) % len(pool)
    return pool[idx]
