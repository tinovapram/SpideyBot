"""TeraBox native resolver — account-pool login, share parsing, and direct download.

Behaviour is grounded in the V5 `downloader/site/terabox.py` and `downloader/terabox.py`
rather than re-derived from scratch. The extraction chain is:

  /s/{short} or /sharing/link?surl={short}
    → resolve short link to full share page
    → parse `fn%22%3A%22` / `fs%22%3A` / `dp%22%3A` for file list (name, size, dlink)
    → sign each dlink with `sign=...&expires=...` (requires logged-in cookies)
    → return direct URLs as single-file transfers

Cookies are mandatory: the signed links only work with a valid `ndus`/`ndut` pair.
The caller supplies a cookie file via `ChainContext.cookies_file` (D8, FR-14).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any

from curl_cffi import requests

from downloader.contracts import Resolution, ResolvedFile, ResolveError, Transfer
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

# Hosts TeraBox answers on. Suffix matching, so `m.terabox.com` and the regional mirrors both work
# and a lookalike does not (`terabox.com.evil.test`).
_HOSTS = (
    "terabox.com",
    "terabox.app",
    "terabox.link",
    "terabox.jp",
    "terabox.cn",
    "1024terabox.com",
    "terasharelink.com",
    "freeterabox.com",
)

# Embedded JSON markers in the share page.
_FN_RE = re.compile(r'fn%22%3A%22([^%]+)%22')
_FS_RE = re.compile(r'fs%22%3A(\d+)')
_DP_RE = re.compile(r'dp%22%3A%22([^%]+)%22')

# Fallback: the newer `filelist` JSON blob.
_FILELIST_RE = re.compile(r'"filelist"\s*:\s*(\[.*?\])', re.DOTALL)

DEFAULT_TIMEOUT = bridge.DEFAULT_TIMEOUT


def matches(url: str) -> bool:
    """Whether this resolver handles the link."""
    return bridge.host_matches(url, _HOSTS)


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolution:
    """Resolve a TeraBox share into direct file transfers."""
    if cookies_file is None or not cookies_file.exists():
        raise ResolveError("TeraBox requires a logged-in cookie file (ndus/ndut)")

    client = bridge.session_for(timeout)
    try:
        await bridge.load_cookies(client, cookies_file, default_domain=".terabox.com")

        # Step 1: resolve short link to full share page
        share_url = await _resolve_share_url(client, url)
        if not share_url:
            raise ResolveError("Could not resolve TeraBox share URL")

        # Step 2: fetch share page and extract file list
        resp = await client.get(share_url)
        html = resp.text

        files = _extract_filelist(html)
        if not files:
            raise ResolveError("No files found in TeraBox share")

        # Step 3: sign each dlink (requires ndus/ndut cookies)
        resolved: list[ResolvedFile] = []
        for item in files:
            signed = await _sign_dlink(client, item["dlink"])
            if not signed:
                log.warning("Failed to sign TeraBox dlink for %s", item["name"])
                continue
            resolved.append(
                ResolvedFile(
                    transfers=(Transfer(url=signed, role="file", filesize=item["size"]),),
                    filename=item["name"],
                    filesize=item["size"],
                    source_url=share_url,
                )
            )

        if not resolved:
            raise ResolveError("Could not sign any TeraBox download links")

        return Resolution(engine="terabox", source_url=url, files=tuple(resolved))
    finally:
        await client.close()


async def _resolve_share_url(client: requests.AsyncSession, url: str) -> str | None:
    """Follow redirects to the canonical share page."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""

    # Already a full share page?
    if "/s/" in parsed.path or "surl=" in parsed.query:
        resp = await client.get(url, allow_redirects=True)
        return str(resp.url)

    # Short link like terabox.com/s/abc123
    if parsed.path.startswith("/s/"):
        resp = await client.get(url, allow_redirects=True)
        return str(resp.url)

    # Try the sharing/link endpoint
    short = parsed.path.rstrip("/").split("/")[-1]
    if short:
        api_url = f"https://{host}/sharing/link?surl={short}"
        resp = await client.get(api_url, allow_redirects=True)
        return str(resp.url)

    return None


def _extract_filelist(html: str) -> list[dict[str, Any]]:
    """Extract file metadata from the share page HTML."""
    # Try the embedded JSON blob first (newer layout)
    m = _FILELIST_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
            return [
                {
                    "name": item.get("server_filename", "file"),
                    "size": int(item.get("size", 0)),
                    "dlink": item.get("dlink", ""),
                }
                for item in data
                if item.get("dlink")
            ]
        except json.JSONDecodeError:
            pass

    # Fallback: regex the url-encoded fields
    names = _FN_RE.findall(html)
    sizes = _FS_RE.findall(html)
    dlinks = _DP_RE.findall(html)

    if not (names and sizes and dlinks):
        return []

    # Decode URL-encoded names and dlinks
    files = []
    for name, size, dlink in zip(names, sizes, dlinks, strict=False):
        files.append({
            "name": urllib.parse.unquote(name),
            "size": int(size),
            "dlink": urllib.parse.unquote(dlink),
        })
    return files


async def _sign_dlink(client: requests.AsyncSession, dlink: str) -> str | None:
    """Sign a TeraBox direct link using the logged-in session cookies."""
    # The dlink already contains the base URL; we need to hit the sign endpoint
    # or the dlink itself with the cookies will work.
    # V5 pattern: the dlink from the page is already a signed URL when cookies are present.
    # But sometimes we need to call /api/sign to get a fresh signature.
    try:
        # Try the dlink directly first (cookies in session)
        resp = await client.head(dlink, allow_redirects=True)
        if resp.status_code == 200:
            return str(resp.url)

        # Fallback: call sign API
        parsed = urllib.parse.urlparse(dlink)
        params = urllib.parse.parse_qs(parsed.query)
        fs_id = params.get("fsid", [None])[0]
        if not fs_id:
            return None

        sign_url = "https://www.terabox.com/api/sign"
        sign_resp = await client.post(sign_url, data={"fsid": fs_id})
        data = sign_resp.json()
        if data.get("sign"):
            return data["sign"]
    except Exception as exc:
        log.debug("TeraBox sign failed: %s", exc)
    return None


def resolver(cookies_file: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> Resolver:
    """Factory for the registry."""
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="terabox", attempt=_attempt)