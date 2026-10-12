"""StreamTape native resolver — norobotlink → get_video redirect.

Behaviour grounded in V5 `downloader/site/streamtape.py`. Extraction chain:

  /v/{id} or /e/{id}
    → fetch page
    → extract `norobotlink` and `token` from packed JS
    → GET `/get_video?token={token}&stream=1`
    → follow redirect to direct .mp4 URL
    → return as single-file transfer
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

_HOST_RE = re.compile(r"streamtape\.(com|to|co|cc|site|net|xyz)")

# Packed JS patterns
_NO_ROBOT_LINK_RE = re.compile(r'norobotlink\s*=\s*["\']([^"\']+)')
_TOKEN_RE = re.compile(r'token\s*=\s*["\']([^"\']+)')
_ROBOT_LINK_RE = re.compile(r'robotlink\s*=\s*["\']([^"\']+)')

DEFAULT_TIMEOUT = bridge.DEFAULT_TIMEOUT


def _host_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


def matches(url: str) -> bool:
    return bool(_HOST_RE.search(_host_of(url)))


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> Resolution:
    session = client or bridge.session_for(timeout, referer="https://streamtape.com/")
    try:
        await bridge.load_cookies(session, cookies_file, default_domain=".streamtape.com")

        # Step 1: fetch page
        html = await bridge.get_text(session, url)

        if "File not found" in html or "deleted" in html.lower():
            raise ResolveError("File not found or deleted on StreamTape")

        # Step 2: extract norobotlink and token
        norobotlink = _extract_norobotlink(html)
        token = _extract_token(html)

        if not norobotlink or not token:
            raise ResolveError("Could not extract StreamTape norobotlink/token")

        # Step 3: call get_video endpoint
        parsed = urllib.parse.urlparse(norobotlink)
        base = f"{parsed.scheme}://{parsed.hostname}"
        get_video_url = f"{base}/get_video?token={token}&stream=1"

        # Step 4: follow redirect to final URL
        head = await session.head(get_video_url, allow_redirects=True)
        final_url = str(head.url)

        filename = bridge.filename_for(
            _extract_title(html) or "streamtape_video", extension=".mp4"
        )

        return bridge.resolution(
            "streamtape", url, [bridge.media(final_url, filename, role="video", source_url=url)]
        )
    finally:
        if client is None:
            await bridge.close(session)


def _extract_norobotlink(html: str) -> str | None:
    m = _NO_ROBOT_LINK_RE.search(html)
    if m:
        return m.group(1)
    m = _ROBOT_LINK_RE.search(html)
    if m:
        return m.group(1)
    return None


def _extract_token(html: str) -> str | None:
    m = _TOKEN_RE.search(html)
    if m:
        return m.group(1)
    return None


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        title = m.group(1).strip()
        for suffix in (" - Streamtape", " | Streamtape"):
            title = title.replace(suffix, "")
        return title
    return None


def resolver(
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="streamtape", attempt=_attempt)