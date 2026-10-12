"""YouTube native resolver — yt-dlp library only (D5).

The V5 implementation used a vidssave.com token API with yt-dlp fallback.
Per D5 and the plan, we use **only the yt-dlp library** — no third-party token APIs.
The selection policy (compatible-first, D28) is applied in :mod:`downloader.selection`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from downloader.contracts import Resolution
from downloader.flow import Resolver
from downloader.probe import resolve_ytdlp
from downloader.sites import bridge

log = logging.getLogger(__name__)

#: The YouTube family: `youtube-nocookie.com` is the same library behind a different host.
_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def matches(url: str) -> bool:
    """Whether this resolver handles the link — YouTube hosts only."""
    return bridge.host_matches(url, _HOSTS)


async def attempt(
    url: str,
    *,
    preference: str = "compatible",
    timeout: float = 180.0,
    cookies_file: Path | None = None,
) -> Resolution:
    """Resolve through yt-dlp library, applying D28 selection."""
    return await resolve_ytdlp(
        url,
        preference=preference,  # type: ignore[arg-type]
        timeout=timeout,
        cookies_file=cookies_file,
    )


def resolver(
    preference: str = "compatible",
    timeout: float = 180.0,
    cookies_file: Path | None = None,
) -> Resolver:
    """Factory for the registry."""
    async def _attempt(url: str) -> Resolution:
        return await attempt(
            url,
            preference=preference,
            timeout=timeout,
            cookies_file=cookies_file,
        )
    return Resolver(name="youtube", attempt=_attempt)