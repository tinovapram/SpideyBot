"""Vidara native resolver — regex + fingerprint probe → /api/stream.

Behaviour grounded in V5 `downloader/site/vidara.py`. Extraction chain:

  /v/{id} or /e/{id}
    → fetch page
    → extract video ID and fingerprint
    → POST to /api/stream with fingerprint
    → extract source URL from response
    → return as single-file transfer
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import Path

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

_HOST_RE = re.compile(r"vidara\.(com|to|co|cc|site|net|xyz)")

# Page patterns
_VIDEO_ID_RE = re.compile(r'videoId["\']?\s*[:=]\s*["\']([^"\']+)')
_FINGERPRINT_RE = re.compile(r'fingerprint["\']?\s*[:=]\s*["\']([^"\']+)')
_SOURCE_RE = re.compile(r'source["\']?\s*[:=]\s*["\']([^"\']+)')

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
) -> Resolution:
    client = bridge.session_for(timeout, referer="https://vidara.com/")
    try:
        await bridge.load_cookies(client, cookies_file, default_domain=".vidara.com")

        # Step 1: fetch page
        html = await bridge.get_text(client, url)

        if "File not found" in html or "deleted" in html.lower():
            raise ResolveError("File not found or deleted on Vidara")

        # Step 2: extract video ID and fingerprint
        video_id = _extract_video_id(html)
        fingerprint = _extract_fingerprint(html)

        if not video_id or not fingerprint:
            raise ResolveError("Could not extract Vidara videoId/fingerprint")

        # Step 3: call /api/stream API
        parsed = urllib.parse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        api_url = f"{base}/api/stream"
        result = await bridge.post_json(
            client, api_url, json_body={"videoId": video_id, "fingerprint": fingerprint}
        )

        if not result.get("success") or not result.get("source"):
            raise ResolveError(f"Vidara API failed: {result.get('msg', 'unknown error')}")

        direct_url = result["source"]

        # Step 4: name the file from the page title
        filename = bridge.filename_for(_extract_title(html) or "vidara_video", extension=".mp4")

        return bridge.resolution(
            "vidara", url, [bridge.media(direct_url, filename, role="video", source_url=url)]
        )
    finally:
        await bridge.close(client)


def _extract_video_id(html: str) -> str | None:
    m = _VIDEO_ID_RE.search(html)
    if m:
        return m.group(1)
    return None


def _extract_fingerprint(html: str) -> str | None:
    m = _FINGERPRINT_RE.search(html)
    if m:
        return m.group(1)
    return None


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        title = m.group(1).strip()
        for suffix in (" - Vidara", " | Vidara"):
            title = title.replace(suffix, "")
        return title
    return None


def resolver(
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="vidara", attempt=_attempt)