"""Doodstream native resolver — embed resolution, pass_md5, CDN redirect.

Behaviour grounded in V5 `downloader/site/doodstream.py`. Extraction chain:

  /d/{id} or /e/{id}
    → fetch embed page
    → extract `pass_md5` token from page
    → POST to `/api/stream/getlink` with pass_md5
    → follow CDN redirect to direct .mp4 URL
    → return as single-file transfer

CAPTCHA and rate limiting are handled by the fetcher's retry/impersonate.
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

_HOST_RE = re.compile(r"dood\.(stream|watch|to|co|la|pm|re|so|ws|cc|pm|la|to|co|watch|stream)")

# Embed page patterns
_PASS_MD5_RE = re.compile(r'pass_md5["\']?\s*[:=]\s*["\']([^"\']+)')
_TOKEN_RE = re.compile(r'token["\']?\s*[:=]\s*["\']([^"\']+)')
_EXPIRE_RE = re.compile(r'expire["\']?\s*[:=]\s*["\']([^"\']+)')

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
    session = client or bridge.session_for(timeout, referer="https://dood.stream/")
    try:
        await bridge.load_cookies(session, cookies_file, default_domain=".dood.stream")

        # Step 1: normalize to embed URL
        parsed = urllib.parse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        video_id = parsed.path.rstrip("/").split("/")[-1]
        embed_url = f"{base}/e/{video_id}"

        # Step 2: fetch embed page
        html = await bridge.get_text(session, embed_url)

        if "File was deleted" in html or "file not found" in html.lower():
            raise ResolveError("File was deleted or not found on Doodstream")

        # Step 3: extract pass_md5 token
        pass_md5 = _extract_pass_md5(html)
        if not pass_md5:
            raise ResolveError("Could not extract Doodstream pass_md5 token")

        # Step 4: call getlink API
        api_url = f"{base}/api/stream/getlink"
        result = await bridge.post_json(session, api_url, json_body={"pass_md5": pass_md5})

        if not result.get("success") or not result.get("result"):
            raise ResolveError(f"Doodstream API failed: {result.get('msg', 'unknown error')}")

        # Step 5: extract direct URL from result
        direct_url = result["result"].get("url") or result["result"].get("direct_url")
        if not direct_url:
            raise ResolveError("No direct URL in Doodstream API response")

        # Step 6: follow redirect to get final URL (CDN)
        head = await session.head(direct_url, allow_redirects=True)
        final_url = str(head.url)

        filename = bridge.filename_for(_extract_title(html) or video_id, extension=".mp4")

        return bridge.resolution(
            "doodstream", url, [bridge.media(final_url, filename, role="video", source_url=url)]
        )
    finally:
        if client is None:
            await bridge.close(session)


def _extract_pass_md5(html: str) -> str | None:
    # Try multiple patterns
    for pattern in (_PASS_MD5_RE, _TOKEN_RE):
        m = pattern.search(html)
        if m:
            return m.group(1)
    return None


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        title = m.group(1).strip()
        for suffix in (" - Doodstream", " | Doodstream", " - Dood.watch"):
            title = title.replace(suffix, "")
        return title
    return None


def resolver(
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="doodstream", attempt=_attempt)