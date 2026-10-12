"""MixDrop native resolver — unpacked JS → MDCore.wurl + mirrors.

Behaviour grounded in V5 `downloader/site/mixdrop.py`. Extraction chain:

  /f/{id} or /e/{id}
    → fetch page
    → extract packed JS (eval(function(p,a,c,k,e,d)...))
    → unpack to find `MDCore.wurl` and mirror array
    → return direct URL(s) as single-file transfer
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

_HOST_RE = re.compile(r"mixdrop\.(co|to|cc|site|net|xyz|ws|ag|sx)")

# Packed JS patterns
_MDCORE_WURL_RE = re.compile(r'MDCore\.wurl\s*=\s*["\']([^"\']+)')
_MIRRORS_RE = re.compile(r'mirrors\s*=\s*(\[.*?\])', re.DOTALL)
_PACKED_JS_RE = re.compile(r'eval\(function\(p,a,c,k,e,d\)', re.DOTALL)

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
    session = client or bridge.session_for(timeout, referer="https://mixdrop.co/")
    try:
        await bridge.load_cookies(session, cookies_file, default_domain=".mixdrop.co")

        # Step 1: fetch page
        html = await bridge.get_text(session, url)

        if "File not found" in html or "deleted" in html.lower():
            raise ResolveError("File not found or deleted on MixDrop")

        # Step 2: extract direct URL from MDCore.wurl
        direct_url = _extract_direct_url(html)
        if not direct_url:
            raise ResolveError("Could not extract MixDrop direct URL")

        # Step 3: name the file from the page title
        filename = bridge.filename_for(_extract_title(html) or "mixdrop_video", extension=".mp4")

        return bridge.resolution(
            "mixdrop", url, [bridge.media(direct_url, filename, role="video", source_url=url)]
        )
    finally:
        if client is None:
            await bridge.close(session)


def _extract_direct_url(html: str) -> str | None:
    # Try direct MDCore.wurl first
    m = _MDCORE_WURL_RE.search(html)
    if m:
        return m.group(1)

    # Try to find and unpack packed JS
    packed_match = _PACKED_JS_RE.search(html)
    if packed_match:
        # Find the full packed function
        start = packed_match.start()
        # Find the closing )
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
        # Unpack it
        unpacked = _unpack_js(packed_code)
        if unpacked:
            m = _MDCORE_WURL_RE.search(unpacked)
            if m:
                return m.group(1)

    return None


def _unpack_js(packed: str) -> str | None:
    """Unpack eval(function(p,a,c,k,e,d)) packed JavaScript."""
    try:
        # Pattern: eval(function(p,a,c,k,e,d){...})
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

        # Build dictionary
        d = {}
        for i in range(c):
            if i < len(k) and k[i]:
                d[_to_base(i, a)] = k[i]

        # Replace in body
        def replace(match):
            word = match.group(0)
            return d.get(word, word)

        result = re.sub(r'\b\w+\b', replace, body)
        return result
    except Exception:
        return None


def _to_base(num: int, base: int) -> str:
    """Convert number to base representation."""
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
        for suffix in (" - Mixdrop", " | Mixdrop"):
            title = title.replace(suffix, "")
        return title
    return None


def resolver(
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="mixdrop", attempt=_attempt)