"""Vidmonstr native resolver — iframeId/token → stream.php → source.

Behaviour grounded in V5 `downloader/site/vidmonstr.py`. Extraction chain:

  /v/{id} or /e/{id}
    → fetch page
    → extract iframeId and token
    → POST to /api/stream.php with iframeId and token
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

_HOST_RE = re.compile(r"vidmonstr\.(com|to|co|cc|site|net|xyz)")

# Page patterns
_IFRAME_ID_RE = re.compile(r'iframeId["\']?\s*[:=]\s*["\']([^"\']+)')
_TOKEN_RE = re.compile(r'token["\']?\s*[:=]\s*["\']([^"\']+)')
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
    client = bridge.session_for(timeout, referer="https://vidmonstr.com/")
    try:
        await bridge.load_cookies(client, cookies_file, default_domain=".vidmonstr.com")

        # Step 1: fetch page
        html = await bridge.get_text(client, url)

        if "File not found" in html or "deleted" in html.lower():
            raise ResolveError("File not found or deleted on Vidmonstr")

        # Step 2: extract iframeId and token
        iframe_id = _extract_iframe_id(html)
        token = _extract_token(html)

        if not iframe_id or not token:
            raise ResolveError("Could not extract Vidmonstr iframeId/token")

        # Step 3: call stream.php API
        parsed = urllib.parse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        api_url = f"{base}/api/stream.php"
        result = await bridge.post_json(
            client, api_url, data={"iframeId": iframe_id, "token": token}
        )

        if not result.get("success") or not result.get("source"):
            raise ResolveError(f"Vidmonstr API failed: {result.get('msg', 'unknown error')}")

        direct_url = result["source"]

        # Step 4: name the file from the page title
        filename = bridge.filename_for(
            _extract_title(html) or "vidmonstr_video", extension=".mp4"
        )

        return bridge.resolution(
            "vidmonstr", url, [bridge.media(direct_url, filename, role="video", source_url=url)]
        )
    finally:
        await bridge.close(client)


def _extract_iframe_id(html: str) -> str | None:
    m = _IFRAME_ID_RE.search(html)
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
        for suffix in (" - Vidmonstr", " | Vidmonstr"):
            title = title.replace(suffix, "")
        return title
    return None


def resolver(
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="vidmonstr", attempt=_attempt)