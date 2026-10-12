"""StreamWish native resolver — packed JS → HLS URL.

Behaviour grounded in V5 `downloader/site/streamwish.py`. Extraction chain:

  /f/{id} or /e/{id}
    → fetch page
    → extract packed JS, unpack it to find the HLS playlist URL
    → hand the playlist to the fetcher as an ``hls`` transfer, which consumes it with ffmpeg

The playlist is not a file: an ``.m3u8`` is a manifest, so it is resolved as an ``hls`` transfer
rather than fetched as bytes that would land on disk as a text file named ``.mp4``.
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

_HOST_RE = re.compile(r"streamwish\.(com|to|co|cc|site|net|xyz)")

# Packed JS patterns
_PACKED_JS_RE = re.compile(r'eval\(function\(p,a,c,k,e,d\)', re.DOTALL)
_M3U8_RE = re.compile(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']')

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
    session = client or bridge.session_for(timeout, referer="https://streamwish.com/")
    try:
        await bridge.load_cookies(session, cookies_file, default_domain=".streamwish.com")

        # Step 1: fetch page
        html = await bridge.get_text(session, url)

        if "File not found" in html or "deleted" in html.lower():
            raise ResolveError("File not found or deleted on StreamWish")

        # Step 2: extract HLS URL from packed JS
        hls_url = _extract_hls_url(html)
        if not hls_url:
            raise ResolveError("Could not extract StreamWish HLS URL")

        # Step 3: name the file from the page title
        filename = bridge.filename_for(_extract_title(html) or "streamwish_video", extension=".mp4")

        return bridge.resolution(
            "streamwish", url, [bridge.media(hls_url, filename, role="hls", source_url=url)]
        )
    finally:
        if client is None:
            await bridge.close(session)


def _extract_hls_url(html: str) -> str | None:
    # Try direct m3u8 first
    m = _M3U8_RE.search(html)
    if m:
        return m.group(1)

    # Try to find and unpack packed JS
    packed_match = _PACKED_JS_RE.search(html)
    if packed_match:
        start = packed_match.start()
        brace_count = 0
        end = start
        for i, ch in enumerate(html[start:], start):
            if ch == '(':
                brace_count += 1
            elif ch == ')':
                brace_count -= 1
                if brace_count == 0:
                    end = i + 1
                    break
        packed_code = html[start:end]
        unpacked = _unpack_js(packed_code)
        if unpacked:
            m = _M3U8_RE.search(unpacked)
            if m:
                return m.group(1)

    return None


def _unpack_js(packed: str) -> str | None:
    """Unpack eval(function(p,a,c,k,e,d)) packed JavaScript."""
    try:
        m = re.search(
            r"eval\(function\(p,a,c,k,e,d\)\{(.*?)\}\)\('(.*?)',(\d+),(\d+),'([^']*)'\.split\('\|'\)",
            packed,
            re.DOTALL,
        )
        if not m:
            return None

        body, p, a, c, k = m.groups()
        a = int(a)
        c = int(c)
        k = k.split('|')

        d = {}
        for i in range(c):
            if i < len(k) and k[i]:
                d[_to_base(i, a)] = k[i]

        def replace(match):
            word = match.group(0)
            return d.get(word, word)

        result = re.sub(r'\b\w+\b', replace, body)
        return result
    except Exception:
        return None


def _to_base(num: int, base: int) -> str:
    if num == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = ""
    while num > 0:
        result = digits[num % base] + result
        num //= base
    return result


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        title = m.group(1).strip()
        for suffix in (" - StreamWish", " | StreamWish"):
            title = title.replace(suffix, "")
        return title
    return None


def resolver(
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="streamwish", attempt=_attempt)