"""
TeraBox Downloader Class
========================
A self-contained Python class for resolving and downloading files from TeraBox
shared links. Based on the TeraBridge-api project and the official 1024TeraBox
REST API documentation.

Sources:
    - https://github.com/saahiyo-cloud/TeraBridge-api
    - https://herobenhero.github.io/1024TeraBox-REST-API/index.html

Authentication:
    TeraBox uses browser-based session cookies and dynamic tokens:
    1. Cookie (ndus) — primary session identifier
    2. jsToken — dynamic security token from client-side JS, required for
       nearly every read/write operation
    3. bdstoken — session-bound token for write operations (upload, delete,
       rename, move, transfer)

    The class can auto-resolve jsToken and bdstoken from an active session
    cookie by scraping the TeraBox main page.

Usage:
    from terabox_downloader import TeraBoxDownloader

    # Initialize with just the ndus cookie (tokens auto-resolved)
    tb = TeraBoxDownloader(cookie="ndus=YOUR_NDUS_VALUE; PANWEB=1")

    # Or provide all tokens explicitly
    tb = TeraBoxDownloader(
        cookie="ndus=YOUR_NDUS; PANWEB=1",
        js_token="YOUR_JSTOKEN",
        bds_token="YOUR_BDSTOKEN",
    )

    # Resolve a share link to get direct download URLs
    result = tb.resolve("https://terabox.com/s/1ABCDEFG...")

    # Stream mode — get HLS manifest info
    result = tb.resolve("https://terabox.com/s/1ABCDEFG...", mode="stream")

    # List files only (no transfer)
    result = tb.resolve("https://terabox.com/s/1ABCDEFG...", mode="list")

    # Download files to disk
    tb.download("https://terabox.com/s/1ABCDEFG...", output_dir="./downloads")

    # Callable shorthand
    result = tb("https://terabox.com/s/1ABCDEFG...")
"""

import aiohttp
import asyncio
import json
import urllib.parse
import re
import os
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple, Callable

import structlog

# curl_cffi (Chrome TLS impersonation) — used by Method A (XDOWNDER-style
# direct-share resolution). Optional at import time; features degrade
# gracefully when it is missing.
try:
    from curl_cffi import requests as _cffi_requests
    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover - import not always available
    _cffi_requests = None
    HAS_CURL_CFFI = False

# Auto-load .env file if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ─── Data Classes ────────────────────────────────────────────────────

@dataclass
class TeraBoxFile:
    """Represents a single file resolved from a TeraBox share link."""
    filename: str
    size_bytes: int
    size_mb: float
    fs_id: str
    dlink: Optional[str] = None
    stream_url: Optional[str] = None
    stream_ready: bool = False
    stream_m3u8: Optional[str] = None
    transfer_status: str = "pending"
    error: Optional[str] = None
    is_dir: bool = False
    category: int = 0
    md5: Optional[str] = None
    thumbs: Optional[Dict[str, str]] = None
    path: Optional[str] = None
    server_mtime: Optional[int] = None
    # Which backend produced the dlink: "account" (Method B, after transfer)
    # or "direct" (Method A, XDOWNDER route=share — needs curl_cffi download).
    backend: str = "account"

    def __repr__(self):
        status = f"[{self.transfer_status}]"
        size = f"{self.size_mb:.2f} MB"
        return f"TeraBoxFile({status} {self.filename!r}, {size})"


@dataclass
class TeraBoxResult:
    """Result of resolving a TeraBox share link."""
    status: str  # "success", "error", "transcoding"
    title: Optional[str] = None
    share_id: Optional[int] = None
    uk: Optional[int] = None
    files: List[TeraBoxFile] = field(default_factory=list)
    error: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    # Resolution method used: "direct" (Method A) or "account" (Method B).
    method: str = "account"

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def download_links(self) -> List[str]:
        """Get all available direct download links."""
        return [f.dlink for f in self.files if f.dlink]

    def __repr__(self):
        if self.ok:
            return f"TeraBoxResult(success, {len(self.files)} file(s), title={self.title!r})"
        return f"TeraBoxResult({self.status}, error={self.error!r})"


# ─── Exceptions ──────────────────────────────────────────────────────

class TeraBoxError(Exception):
    """Base exception for TeraBox downloader errors."""
    pass


class TeraBoxAuthError(TeraBoxError):
    """Raised when authentication/session is invalid or tokens cannot be resolved."""
    pass


class TeraBoxURLError(TeraBoxError):
    """Raised when the share URL is invalid or cannot be parsed."""
    pass


class TeraBoxAPIError(TeraBoxError):
    """Raised when the TeraBox API returns an error."""
    def __init__(self, message: str, errno: int = None):
        super().__init__(message)
        self.errno = errno


# ─── Main Downloader Class ───────────────────────────────────────────

class TeraBoxDownloader:
    """
    TeraBox file downloader and link resolver.

    Resolves shared TeraBox links into direct download URLs and HLS stream
    manifests. Handles session token auto-resolution, file transfer to
    account storage, and download link generation.

    API Reference:
        Base URL: https://www.1024terabox.com  (or dm.1024terabox.com)

        Authentication (3 tokens needed):
            - ndus cookie  → session identifier
            - jsToken      → dynamic JS token (auto-resolved from /main page)
            - bdstoken     → session-bound token (auto-resolved from /main page)

        Flow to download from a share link:
            1. GET /share/list?shorturl={surl}    → get shareid, uk, file list
            2. POST /share/transfer               → copy file to own storage
            3. GET /rest/2.0/pcs/file?method=locdownload&fidlist=[{fs_id}]
               → get dlink (temporary download URL)
            4. GET {dlink} with User-Agent header  → download binary

    Args:
        cookie:    TeraBox cookie string containing ndus (or env TERABOX_COOKIE)
        js_token:  JS token (or env TERABOX_JSTOKEN). Auto-resolved if None.
        bds_token: BDS token (or env TERABOX_BDSTOKEN). Auto-resolved if None.
        sign:      Request signature (or env TERABOX_SIGN)
        timestamp: Request timestamp (or env TERABOX_TIMESTAMP)
        logid:     Log ID (or env TERABOX_LOGID)
        root_path: Account folder to copy shared files into before downloading
                    (default: /downloads, overridden per-user in handler)
        timeout:   HTTP request timeout in seconds (default: 30)
        auto_resolve_tokens: If True, auto-resolve jsToken and bdstoken
                             from session when not provided (default: True)
        max_retries:      Max retry attempts for failed requests (default: 3)
        backoff_factor:   Exponential backoff factor (default: 0.5)
        pool_connections: Number of pooled connections (default: 10)
        pool_maxsize:     Max size of connection pool (default: 20)
    """

    # ─── Constants ───────────────────────────────────────────────────

    BASE_URL = "https://www.terabox.com"
    BASE_API = "https://dm.terabox.com"
    APP_ID = "250528"
    USER_AGENT = (
        "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )

    # surl parsing constants
    _SURL_MIN_LEN = 8
    _LEADING_ONE_MAX_STRIPS = 4
    _VALID_SURL = re.compile(r"^[A-Za-z0-9_-]+$")

    # Patterns for scraping tokens from the main page HTML/JS
    _JSTOKEN_PATTERNS = [
        re.compile(r'jsToken["\']?\s*[:=]\s*["\']([A-F0-9]{64,})["\']', re.IGNORECASE),
        re.compile(r'fn\("([A-F0-9]{64,})"\)', re.IGNORECASE),
    ]
    _BDSTOKEN_PATTERN = re.compile(
        r'bdstoken["\']?\s*[:=]\s*["\']([a-f0-9]{32})["\']', re.IGNORECASE
    )
    _LOGID_PATTERN = re.compile(
        r'logid["\']?\s*[:=]\s*["\'](\d{15,})["\']', re.IGNORECASE
    )
    _SIGN_PATTERN = re.compile(
        r'sign["\']?\s*[:=]\s*["\']([A-Za-z0-9%+=/_-]{20,})["\']', re.IGNORECASE
    )
    _TIMESTAMP_PATTERN = re.compile(
        r'timestamp["\']?\s*[:=]\s*["\']?(\d{10})["\']?', re.IGNORECASE
    )

    # ── Method A (XDOWNDER direct-share) ────────────────────────────
    # TeraBox's web API/CDN rejects plain aiohttp/requests TLS
    # fingerprints (errno 4000020 "need verify", HTTP 403 on dlinks).
    # Method A impersonates desktop Chrome via curl_cffi and downloads
    # straight from the share — no account storage/transfer needed.
    _DIRECT_BASE_HOSTS = (
        "dm.terabox.app",
        "www.terabox.app",
        "www.1024tera.com",
        "www.terabox.com",
        "www.1024terabox.com",
    )
    _DIRECT_API_HOST = "dm.terabox.app"
    _DIRECT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
    _DIRECT_IMPERSONATE_TARGETS = (
        "chrome110", "chrome120", "chrome124", "chrome131", "chrome",
    )
    _SHARE_JSTOKEN_PATTERNS = [
        re.compile(r'fn%28%22(.*?)%22%29'),
        re.compile(r'fn\"([^\"]+)\"\)'),
        re.compile(r'jsToken\s*=\s*["\']([^"\']+)["\']'),
        re.compile(r'jsToken["\']?\s*:\s*["\']([^"\']+)["\']'),
        re.compile(r'window\.jsToken\s*=\s*["\']([^"\']+)["\']'),
    ]

    def __init__(
        self,
        cookie: str = None,
        js_token: str = None,
        bds_token: str = None,
        sign: str = None,
        timestamp: str = None,
        logid: str = None,
        root_path: str = None,
        timeout: int = 30,
        auto_resolve_tokens: bool = True,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        pool_connections: int = 10,
        pool_maxsize: int = 20,
    ):
        self.logger = structlog.get_logger("TeraBoxDownloader")

        # ── Cookie ───────────────────────────────────────────────────
        self.cookie = cookie or os.environ.get("TERABOX_COOKIE", "")
        if not self.cookie:
            raise TeraBoxAuthError(
                "No cookie provided. Pass cookie= or set TERABOX_COOKIE env var. "
                "The cookie must contain a valid 'ndus' value from your browser session."
            )

        self._cookies_dict = self._parse_cookies(self.cookie)

        # ── Tokens (may be auto-resolved later) ──────────────────────
        self.js_token = js_token or os.environ.get("TERABOX_JSTOKEN", "")
        self.bds_token = bds_token or os.environ.get("TERABOX_BDSTOKEN", "")
        self.sign = sign or os.environ.get("TERABOX_SIGN", "")
        self.timestamp = timestamp or os.environ.get("TERABOX_TIMESTAMP", "")
        self.logid = logid or os.environ.get("TERABOX_LOGID", "")

        # root_path: staging folder on the bot's own TeraBox account.
        # Overridden per-task in terabox_handler to /downloads/{user_id}/terabox.
        self.root_path = root_path or "/downloads"
        self.timeout = timeout
        self._tokens_resolved = False

        # ── Store config for lazy session creation ───────────────────
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._pool_maxsize = pool_maxsize

        # ── HTTP Headers ─────────────────────────────────────────────
        self._headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.8",
            "Referer": f"{self.BASE_API}/main?category=all&path=%2F",
            "X-Requested-With": "XMLHttpRequest",
        }

        # ── Lazy aiohttp session (created on first request) ─────────
        self.session: Optional[aiohttp.ClientSession] = None
        self._auto_resolve_tokens = auto_resolve_tokens
        self._session_lock = asyncio.Lock() if hasattr(asyncio, '_get_running_loop') else None

        # ── Method A (XDOWNDER) lazy curl_cffi session ──────────────
        self._direct_session = None
        self._direct_lock = asyncio.Lock()

    # ─── Session Lifecycle ──────────────────────────────────────────

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Lazily create the aiohttp session on first use."""
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=10,
                limit_per_host=self._pool_maxsize,
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.session = aiohttp.ClientSession(
                headers=self._headers,
                cookies=self._cookies_dict,
                connector=connector,
                timeout=timeout,
            )
            # Auto-resolve tokens on first use
            if self._auto_resolve_tokens and (not self.js_token or not self.bds_token):
                try:
                    await self._resolve_tokens()
                except Exception as e:
                    self.logger.warning(
                        "Auto-resolve tokens failed, manual tokens may be needed",
                        error=str(e),
                    )
        return self.session

    async def close(self):
        """Close the aiohttp session and release resources."""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
        # Close the Method A curl_cffi session if one was created.
        if self._direct_session is not None:
            try:
                await self._direct_session.close()
            except Exception:
                pass
            self._direct_session = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """HTTP request with exponential backoff retry."""
        session = await self._ensure_session()
        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = await session.request(method, url, **kwargs)
                if resp.status in (500, 502, 503, 504) and attempt < self._max_retries - 1:
                    await resp.release()
                    wait = self._backoff_factor * (2 ** attempt)
                    self.logger.debug("HTTP retry", status=resp.status, url=url, wait=f"{wait:.1f}s", attempt=attempt+1)
                    await asyncio.sleep(wait)
                    continue
                return resp
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    wait = self._backoff_factor * (2 ** attempt)
                    self.logger.debug("Request error, retrying", url=url, error=str(e), wait=f"{wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue
                raise
        raise last_exc or RuntimeError("Request failed after retries")

    # ─── Token Auto-Resolution ───────────────────────────────────────

    async def _resolve_tokens(self):
        """
        Scrape jsToken and bdstoken from the TeraBox main page.

        The TeraBox web app embeds these tokens in the page HTML/JS when
        you load the main drive page with a valid session cookie.
        """
        self.logger.info("Auto-resolving tokens from session")

        try:
            r = await self._request_with_retry("GET", f"{self.BASE_API}/main")
            if r.status != 200:
                await r.release()
                raise TeraBoxAuthError(
                    f"Main page returned HTTP {r.status}. Cookie may be invalid."
                )

            html = urllib.parse.unquote(await r.text())
            await r.release()

            # Extract bdstoken
            if not self.bds_token:
                m = self._BDSTOKEN_PATTERN.findall(html)
                if m:
                    self.bds_token = m[0]
                    self.logger.info("Resolved bdstoken", prefix=f"{self.bds_token[:8]}...")
                else:
                    self.logger.warning("Could not find bdstoken in page HTML")

            # Extract jsToken
            if not self.js_token:
                for pattern in self._JSTOKEN_PATTERNS:
                    m = pattern.findall(html)
                    if m:
                        self.js_token = m[0]
                        self.logger.info("Resolved jsToken", prefix=f"{self.js_token[:16]}...")
                        break
                if not self.js_token:
                    self.logger.warning("Could not find jsToken in page HTML")

            # Extract logid (optional)
            if not self.logid:
                m = self._LOGID_PATTERN.findall(html)
                if m:
                    self.logid = m[0]

            # Extract sign (optional)
            if not self.sign:
                m = self._SIGN_PATTERN.findall(html)
                if m:
                    self.sign = m[0]

            # Extract timestamp (optional)
            if not self.timestamp:
                m = self._TIMESTAMP_PATTERN.findall(html)
                if m:
                    self.timestamp = m[0]

            self._tokens_resolved = True
            self.logger.info("Token resolution complete")

        except TeraBoxAuthError:
            raise
        except Exception as e:
            raise TeraBoxAuthError(f"Failed to resolve tokens: {e}")

    # ─── Credential Management ───────────────────────────────────────

    def update_credentials(
        self,
        cookie: str = None,
        js_token: str = None,
        bds_token: str = None,
        sign: str = None,
        timestamp: str = None,
        logid: str = None,
    ):
        """Update session credentials dynamically."""
        if cookie:
            self.cookie = cookie
            self._cookies_dict = self._parse_cookies(cookie)
            if self.session and not self.session.closed:
                # aiohttp cookies are immutable after creation; recreate is simplest
                self.logger.info("Cookie updated, session will be recreated")
                self.session = None
            # Method A curl session must also be rebuilt with the new cookie.
            if self._direct_session is not None:
                try:
                    self.logger.info("Cookie updated, direct session will be recreated")
                except Exception:
                    pass
                self._direct_session = None
        if js_token:
            self.js_token = js_token
        if bds_token:
            self.bds_token = bds_token
        if sign:
            self.sign = sign
        if timestamp:
            self.timestamp = timestamp
        if logid:
            self.logid = logid

    async def validate_session(self) -> Tuple[bool, str]:
        """
        Verify if the current cookie session is valid by checking for a
        bdstoken in the main page response.

        Returns:
            (bool, str): (is_valid, message)
        """
        try:
            r = await self._request_with_retry("GET", f"{self.BASE_API}/main")
            if r.status != 200:
                await r.release()
                return False, f"HTTP status {r.status}"

            text = await r.text()
            await r.release()
            m = self._BDSTOKEN_PATTERN.findall(text)
            if m:
                return True, "Valid"
            return False, "bdstoken not found (session likely expired or invalid)"
        except Exception as e:
            return False, f"Request failed: {str(e)}"



    # ─── URL Parsing ─────────────────────────────────────────────────

    @classmethod
    def parse_surl(cls, url: str) -> str:
        """
        Extract and clean the shorturl key (surl) from a TeraBox share link.

        Recognized formats:
          - https://terabox.com/s/1ABCDEFG...
          - https://1024terabox.com/s/1ABCDEFG...
          - https://terabox.com/share/list?surl=ABCDEFG...
          - https://terabox.com/s/1ABCDEFG?fid=...
          - https://terabox.com/api/shorturlinfo?ABCDEFG...
          - ABCDEFG...  (bare surl)

        Returns:
            str: Cleaned surl string

        Raises:
            TeraBoxURLError: If URL is invalid or surl cannot be extracted
        """
        if not isinstance(url, str) or not url:
            raise TeraBoxURLError("Empty or non-string input")

        surl = None

        # /api/shorturlinfo?SURL form: the surl is the bare query string
        # e.g. https://www.terabox.com/api/shorturlinfo?1Apys2PVtkAx31zVCeVSSDA
        if "/api/shorturlinfo?" in url:
            after = url.split("/api/shorturlinfo?", 1)[1]
            # Take everything up to a & or # (in case of extra params)
            surl = after.split("&", 1)[0].split("#", 1)[0].strip()
        # Query form: ?surl=...
        elif "surl=" in url:
            after = url.split("surl=", 1)[1]
            surl = after.split("&", 1)[0]
        elif "/s/" in url:
            after = url.split("/s/", 1)[1]
            surl = after.split("?", 1)[0].split("#", 1)[0]
        else:
            stripped = url.strip()
            if "://" in stripped or "/" in stripped or "." in stripped:
                raise TeraBoxURLError(f"No surl marker found in {url!r}")
            if stripped.startswith("http"):
                raise TeraBoxURLError(f"Malformed input {url!r}")
            if cls._VALID_SURL.match(stripped) and len(stripped) >= cls._SURL_MIN_LEN:
                surl = stripped

        if not surl:
            raise TeraBoxURLError(f"No surl found in {url!r}")

        # Drop any trailing path component
        surl = surl.rstrip("/").split("/")[-1]

        if not cls._VALID_SURL.match(surl):
            raise TeraBoxURLError(
                f"Extracted value {surl!r} contains invalid characters"
            )

        # Leading-'1' strip: TeraBox convention where /s/1ABC... and ?surl=ABC...
        # resolve to the same share (the URL-path form prepends '1')
        if len(surl) > 22 and surl.startswith("1"):
            for _ in range(cls._LEADING_ONE_MAX_STRIPS):
                if (
                    not surl.startswith("1")
                    or len(surl) - 1 < cls._SURL_MIN_LEN
                    or len(surl) <= 22
                ):
                    break
                surl = surl[1:]

        if len(surl) < cls._SURL_MIN_LEN:
            raise TeraBoxURLError(
                f"Cleaned surl {surl!r} is shorter than the "
                f"{cls._SURL_MIN_LEN}-char minimum"
            )

        return surl

    # ─── Core Resolution Logic ───────────────────────────────────────

    def _query_params(self) -> str:
        """Build common query parameters string for API requests."""
        params = (
            f"app_id={self.APP_ID}&web=1&channel=dubox&clienttype=0"
        )
        if self.js_token:
            params += f"&jsToken={self.js_token}"
        if self.logid:
            params += f"&dp-logid={self.logid}"
        return params

    async def _get_share_list(
        self, surl: str, dir_path: str = "/", root: int = 1
    ) -> Dict[str, Any]:
        """
        Step 1: Resolve a share link to get shareid, uk, and file list.

        Endpoint: GET /share/list
        (was previously /api/shorturlinfo — /share/list is the documented
        endpoint and returns the same data plus nested directory contents)

        Args:
            surl: The cleaned surl identifier
            dir_path: The directory path to list (default: root "/")
            root: Root flag (1 for root, 0 for subfolders)

        Returns:
            dict with 'share_id', 'uk', 'title', 'file_list'

        Raises:
            TeraBoxAPIError on failure
        """
        url = (
            f"{self.BASE_API}/share/list"
            f"?{self._query_params()}"
            f"&shorturl={surl}"
            f"&root={root}"
            f"&dir={urllib.parse.quote(dir_path)}"
            f"&order=time&desc=1"
            f"&num=100&page=1"
        )
        self.logger.debug("Resolving share", url=url)
        r = await self._request_with_retry("GET", url)
        try:
            data = await r.json()
        finally:
            await r.release()

        errno = data.get("errno", -1)
        if errno != 0:
            errmsg = data.get("errmsg", "Unknown error")
            raise TeraBoxAPIError(
                f"share/list failed: errno={errno}, errmsg={errmsg}",
                errno=errno,
            )

        file_list = data.get("list") or data.get("file_list") or []
        title = data.get("title", "")

        return {
            "share_id": data.get("shareid") or data.get("share_id"),
            "uk": data.get("uk"),
            "title": title,
            "file_list": file_list,
            "raw": data,
        }



    async def _list_dir(self, path: str) -> List[Dict]:
        """
        List files in a directory in the user's own storage.

        Endpoint: GET /api/list

        Args:
            path: Directory path (e.g. "/cloudvids")

        Returns:
            List of file metadata dicts
        """
        url = (
            f"{self.BASE_API}/api/list"
            f"?{self._query_params()}"
            f"&dir={urllib.parse.quote(path)}"
            f"&order=time&desc=1&num=1000"
        )
        try:
            r = await self._request_with_retry("GET", url)
            try:
                data = await r.json()
            finally:
                await r.release()
            if data.get("errno") == 0:
                return data.get("list") or []
            else:
                self.logger.warning(
                    "_list_dir API error", path=path, errno=data.get("errno"), errmsg=data.get("errmsg", "unknown")
                )
        except Exception as e:
            self.logger.warning("_list_dir request failed", path=path, error=str(e))
        return []

    async def _get_existing_files(self) -> Dict[str, Dict[str, Any]]:
        """Get dict of filenames already in the root_path folder mapped to their metadata."""
        items = await self._list_dir(self.root_path)
        return {
            item.get("server_filename"): {
                "fs_id": str(item.get("fs_id", "")),
                "path": item.get("path", ""),
                "size": int(item.get("size", 0))
            }
            for item in items if item.get("server_filename")
        }

    async def _ensure_root_dir(self):
        """Create the root_path directory if it doesn't exist."""
        url = (
            f"{self.BASE_API}/api/create"
            f"?{self._query_params()}"
            f"&bdstoken={self.bds_token}"
        )
        payload = {
            "path": self.root_path,
            "isdir": "1",
            "block_list": "[]",
        }
        try:
            r = await self._request_with_retry("POST", url, data=payload)
            await r.release()
        except Exception as e:
            self.logger.warning("Failed to create root directory", path=self.root_path, error=str(e))  # Directory may already exist

    async def _transfer_file(
        self, share_id: int, uk: int, fs_id: str, filename: str
    ) -> Dict[str, Any]:
        """
        Step 2: Transfer (save) a shared file into the user's own storage.

        Endpoint: POST /share/transfer

        Args:
            share_id: Share identifier from /share/list response
            uk: User key of the share owner
            fs_id: File system ID of the file to transfer
            filename: Original filename

        Returns:
            dict with transfer result info
        """
        url = (
            f"{self.BASE_API}/share/transfer"
            f"?{self._query_params()}"
            f"&shareid={share_id}&from={uk}"
            f"&bdstoken={self.bds_token}"
        )
        payload = {
            "fsidlist": f"[{fs_id}]",
            "path": self.root_path,
        }

        self.logger.debug("Transferring file", filename=filename, fs_id=fs_id)
        r = await self._request_with_retry("POST", url, data=payload)
        try:
            data = await r.json()
        finally:
            await r.release()
        errno = data.get("errno", -1)

        result = {"status": "success", "errno": errno, "raw": data}

        if errno == 0:
            result["status"] = "success"
        elif errno == 12:
            # File already exists in storage — fine
            result["status"] = "already_exists"
        elif errno == -33:
            # Too many files being transferred, already processing
            result["status"] = "already_exists"
        else:
            result["status"] = "error"
            result["error"] = data.get("errmsg") or data.get("show_msg") or f"Transfer errno={errno}"

        return result

    async def _get_download_link(self, fs_id: str) -> Optional[str]:
        """
        Step 3: Resolve the direct download link for a file in account storage.

        Endpoint: GET /api/filemetas
            ?fsids=["fs_id"]

        Args:
            fs_id: File system ID in account storage

        Returns:
            Direct download URL string, or None
        """
        fsids_str = json.dumps([str(fs_id)])
        encoded_fsids = urllib.parse.quote(fsids_str)

        url = (
            f"{self.BASE_API}/api/filemetas"
            f"?{self._query_params()}"
            f"&fsids={encoded_fsids}"
            f"&dlink=1"
            f"&thumb=0"
            f"&bdstoken={self.bds_token}"
        )

        r = await self._request_with_retry("GET", url)
        try:
            data = await r.json()
        finally:
            await r.release()

        if data.get("errno") == 0:
            info = data.get("list") or data.get("info") or []
            if isinstance(info, list) and info:
                return info[0].get("dlink")

        return None

    async def _get_stream_info(
        self,
        path: str,
        wait_for_transcoding: bool = False,
        quality: str = "M3U8_AUTO_480",
    ) -> Dict[str, Any]:
        """
        Get HLS streaming info for a video file.

        Endpoint: GET /api/streaming

        Args:
            path: Full path to the file in account storage
            wait_for_transcoding: Block and poll if transcoding is in progress
            quality: Stream quality ("M3U8_AUTO_480", "M3U8_AUTO_720", etc.)

        Returns:
            dict with 'stream_url', 'stream_ready', 'ltime', and 'stream_m3u8' (if ready)
        """
        url = (
            f"{self.BASE_API}/api/streaming"
            f"?{self._query_params()}"
            f"&path={urllib.parse.quote(path)}"
            f"&type={quality}"
            f"&bdstoken={self.bds_token}"
        )

        max_attempts = 10 if wait_for_transcoding else 1
        for attempt in range(max_attempts):
            r = await self._request_with_retry("GET", url)
            try:
                # If the response contains EXTM3U playlist, it means it is ready and transcoded
                text = await r.text()
                if r.status == 200 and "#EXTM3U" in text:
                    return {
                        "stream_url": url,
                        "stream_ready": True,
                        "stream_m3u8": text,
                    }

                try:
                    data = await r.json()
                    errno = data.get("errno", -1)
                except Exception as json_err:
                    self.logger.debug("Failed to parse stream info response as JSON", error=str(json_err))
                    errno = -1
                    data = {}
            finally:
                await r.release()

            if errno == 0:
                return {
                    "stream_url": data.get("lurl") or data.get("m3u8_url") or url,
                    "stream_ready": True,
                    "ltime": data.get("ltime"),
                }
            elif errno == 130 and wait_for_transcoding:
                # errno 130 = transcoding in progress — wait and retry
                self.logger.info(
                    "Transcoding in progress, waiting",
                    attempt=f"{attempt + 1}/{max_attempts}",
                )
                await asyncio.sleep(5)
            else:
                break

        return {
            "stream_url": None,
            "stream_ready": False,
            "errno": errno,
            "error": data.get("errmsg") if data else f"Streaming error (errno {errno})"
        }

    async def _process_file(
        self,
        item: Dict,
        share_id: int,
        uk: int,
        existing_files: Dict[str, Dict[str, Any]],
        action: str = "download",
        wait_for_transcoding: bool = False,
    ) -> TeraBoxFile:
        """
        Process a single file: transfer it and resolve download/stream links.

        Args:
            item: File metadata dict from share info response
            share_id: Share identifier
            uk: User key of share owner
            existing_files: Dict of already-transferred filenames mapped to metadata
            action: "download", "stream", or "list"
            wait_for_transcoding: Whether to wait for video transcoding

        Returns:
            TeraBoxFile with resolved links
        """
        filename = item.get("server_filename", "unknown")
        fs_id = str(item.get("fs_id", ""))
        size_bytes = int(item.get("size", 0))
        size_mb = round(size_bytes / (1024 * 1024), 2)
        is_dir = int(item.get("isdir", 0)) == 1
        category = int(item.get("category", 0))
        md5 = item.get("md5")
        thumbs = item.get("thumbs")
        path = item.get("path", "")
        server_mtime = item.get("server_mtime")

        result = TeraBoxFile(
            filename=filename,
            size_bytes=size_bytes,
            size_mb=size_mb,
            fs_id=fs_id,
            is_dir=is_dir,
            category=category,
            md5=md5,
            thumbs=thumbs,
            path=path,
            server_mtime=server_mtime,
        )

        if action == "list":
            result.transfer_status = "listed"
            return result

        if is_dir:
            result.transfer_status = "skipped_directory"
            return result

        # Transfer file to account storage (if not already there)
        my_fs_id = fs_id
        try:
            if filename not in existing_files:
                transfer = await self._transfer_file(share_id, uk, fs_id, filename)
                result.transfer_status = transfer["status"]
                if transfer["status"] == "error":
                    result.error = transfer.get("error")
                    return result
                # Resolve the fs_id in our account storage from the transfer response
                try:
                    extra_list = transfer.get("raw", {}).get("extra", {}).get("list", [])
                    if extra_list:
                        to_fs_id = str(extra_list[0].get("to_fs_id", ""))
                        if to_fs_id:
                            my_fs_id = to_fs_id
                except Exception as to_fs_err:
                    self.logger.debug("Failed to extract to_fs_id from transfer response", error=str(to_fs_err))
            else:
                result.transfer_status = "already_exists"
                my_fs_id = existing_files[filename].get("fs_id") or fs_id
        except Exception as e:
            result.transfer_status = "transfer_error"
            result.error = str(e)
            return result

        # Resolve download link
        if action in ("download", "stream"):
            try:
                dlink = await self._get_download_link(my_fs_id)
                result.dlink = dlink
            except Exception as e:
                result.error = f"Failed to get download link: {e}"

        # Resolve stream info for video files (category 1 = video)
        if action == "stream" and category in (1, 3):
            try:
                target_path = f"{self.root_path}/{filename}"
                stream_info = await self._get_stream_info(
                    target_path,
                    wait_for_transcoding=wait_for_transcoding,
                )
                result.stream_url = stream_info.get("stream_url")
                result.stream_ready = stream_info.get("stream_ready", False)
                if "stream_m3u8" in stream_info:
                    result.stream_m3u8 = stream_info.get("stream_m3u8")
            except Exception as e:
                result.error = f"Failed to get stream info: {e}"

        if not result.error:
            result.transfer_status = "success"

        return result

    # ─── Method A helpers (XDOWNDER direct-share) ───────────────────

    @staticmethod
    def _extract_share_jstoken(html_text: str) -> Optional[str]:
        """Extract jsToken from a /sharing/link page (XDOWNDER patterns)."""
        if not html_text:
            return None
        for pat in TeraBoxDownloader._SHARE_JSTOKEN_PATTERNS:
            m = pat.search(html_text)
            if m and m.group(1):
                return m.group(1)
        return None

    async def _ensure_direct_session(self):
        """Lazily build a curl_cffi AsyncSession impersonating Chrome (Method A)."""
        if self._direct_session is not None:
            return self._direct_session
        async with self._direct_lock:
            if self._direct_session is not None:
                return self._direct_session
            if not HAS_CURL_CFFI:
                raise TeraBoxAuthError(
                    "curl_cffi is required for direct (XDOWNDER) resolution"
                )
            last_err = None
            for target in self._DIRECT_IMPERSONATE_TARGETS:
                try:
                    self._direct_session = _cffi_requests.AsyncSession(
                        impersonate=target,
                        cookies=dict(self._cookies_dict),
                    )
                    self.logger.info(
                        "Method A curl session ready", impersonate=target
                    )
                    return self._direct_session
                except Exception as e:
                    last_err = e
                    self._direct_session = None
            raise TeraBoxAuthError(
                f"Could not create curl_cffi impersonation session: {last_err}"
            )

    async def _download_direct_async(self, tb_file: TeraBoxFile, filepath: str) -> str:
        """Download a Method-A (route=share) dlink via curl_cffi impersonation.

        Plain aiohttp gets HTTP 403 from the share CDN; the impersonated
        session (carrying the ndus cookie) is required.

        Uses per-chunk stall detection to abort when the CDN throttles or
        stalls large file transfers.  Also enforces a maximum total
        download time based on file size.
        """
        session = await self._ensure_direct_session()
        headers = {
            "User-Agent": self._DIRECT_UA,
            "Referer": "https://www.terabox.app/",
            "Accept": "*/*",
        }

        # ── Adaptive stall / total-time limits ────────────────────
        size_mb = (tb_file.size_bytes or 0) / (1024 * 1024)
        if size_mb < 50:
            stall_timeout = 120       # small: 2 min per chunk
        elif size_mb < 500:
            stall_timeout = 300       # medium: 5 min
        elif size_mb < 2048:
            stall_timeout = 600       # large: 10 min
        else:
            stall_timeout = 900       # huge: 15 min

        # Minimum ~100 KB/s effective throughput; floor of 10 min.
        max_total = max(600, (tb_file.size_bytes or 0) // (100 * 1024))

        try:
            async with session.stream(
                "GET", tb_file.dlink, headers=headers, timeout=3600
            ) as resp:
                if resp.status_code != 200:
                    raise TeraBoxError(
                        f"Direct download HTTP {resp.status_code} for "
                        f"{tb_file.filename}"
                    )
                with open(filepath, "wb") as f:
                    aiter = resp.aiter_content()
                    start = time.time()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                aiter.__anext__(), timeout=stall_timeout,
                            )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise TeraBoxError(
                                f"Download stalled for {tb_file.filename}: "
                                f"no data received in {stall_timeout}s"
                            )
                        if chunk:
                            f.write(chunk)
                        if time.time() - start > max_total:
                            raise TeraBoxError(
                                f"Download too slow for {tb_file.filename}: "
                                f"exceeded {max_total}s time limit"
                            )
        except Exception:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except OSError:
                pass
            raise
        return filepath

    # ─── Public API ──────────────────────────────────────────────────

    async def resolve(
        self,
        url: str,
        mode: str = "download",
        wait_for_transcoding: bool = False,
    ) -> TeraBoxResult:
        """
        Resolve a TeraBox share link into download/stream URLs.

        ``download`` mode tries **Method A** first (XDOWNDER-style direct
        share — no account storage) and automatically falls back to
        **Method B** (account transfer) when A is unavailable or fails.
        ``stream``/``list`` always use Method B (they need account storage).

        Args:
            url: TeraBox share link (any recognized format)
            mode: "download" (direct links), "stream" (HLS manifest),
                  or "list" (metadata only, no transfer)
            wait_for_transcoding: If True and mode="stream", blocks and
                                  retries when transcoding is in progress

        Returns:
            TeraBoxResult with file info and download links.  ``method``
            is "direct" when Method A succeeded, otherwise "account".
        """
        if mode == "download":
            result = await self.resolve_method_a(url)
            if result is not None and result.ok:
                self.logger.info(
                    "Method A (direct share) resolved",
                    files=len(result.files),
                )
                return result
            self.logger.info(
                "Falling back to Method B (account transfer)", mode=mode
            )
        return await self.resolve_method_b(
            url, mode=mode, wait_for_transcoding=wait_for_transcoding
        )

    # ── Method A — XDOWNDER direct-share (no account transfer) ───────

    async def resolve_method_a(self, url: str) -> Optional[TeraBoxResult]:
        """
        Method A: resolve a share link WITHOUT transferring to an account.

        Mirrors XDOWNDER: impersonate desktop Chrome (curl_cffi), scrape a
        fresh jsToken from the /sharing/link page, then call /share/list
        recursively.  Every file arrives with its own signed ``dlink``
        (route=share) that is downloaded straight from the share CDN.

        Returns:
            TeraBoxResult(method="direct") on success, else None so the
            caller can fall back to Method B.
        """
        if not HAS_CURL_CFFI:
            self.logger.warning("Method A skipped: curl_cffi not installed")
            return None

        try:
            short_url = self.parse_surl(url)
        except Exception as e:
            self.logger.warning("Method A: surl parse failed", error=str(e))
            return None
        # /s/1XXX… path form carries a leading "1"; the sharing page wants
        # that form while /share/list accepts the stripped form (try both).
        surl_param = short_url if short_url.startswith("1") else "1" + short_url

        try:
            session = await self._ensure_direct_session()

            # 1. Scrape a fresh jsToken from a sharing page.
            token = None
            page_headers = {
                "User-Agent": self._DIRECT_UA,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
            for host in self._DIRECT_BASE_HOSTS:
                try:
                    page_url = f"https://{host}/sharing/link?surl={surl_param}"
                    resp = await session.get(
                        page_url, headers=page_headers, timeout=15
                    )
                    if resp.status_code == 200:
                        token = self._extract_share_jstoken(resp.text)
                        if token:
                            self.logger.debug("Method A jsToken source", host=host)
                            break
                except Exception as e:
                    self.logger.debug(
                        "Method A sharing page failed", host=host, error=str(e)
                    )
            if not token:
                self.logger.warning("Method A: no jsToken on any sharing page")
                return None

            # 2. /share/list recursively — files carry their own dlink.
            api_headers = {
                "Host": self._DIRECT_API_HOST,
                "User-Agent": self._DIRECT_UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"https://{self._DIRECT_API_HOST}",
                "Referer": (
                    f"https://{self._DIRECT_API_HOST}/sharing/link"
                    f"?surl={short_url}&clearCache=1"
                ),
            }
            api_url = f"https://{self._DIRECT_API_HOST}/share/list"

            def _params(surl, root, dir_path=None):
                p = {
                    "app_id": "250528",
                    "jsToken": token,
                    "site_referer": "https://www.terabox.app/",
                    "shorturl": surl,
                    "root": str(root),
                }
                if dir_path is not None:
                    p["dir"] = dir_path
                return p

            async def _list(params):
                r = await session.get(
                    api_url, params=params, headers=api_headers, timeout=20
                )
                try:
                    return r.json()
                except Exception:
                    return None

            payload = None
            for cand in (short_url, surl_param):
                payload = await _list(_params(cand, 1))
                if payload and payload.get("errno") == 0:
                    break
            if not payload or payload.get("errno") != 0:
                self.logger.warning(
                    "Method A: share/list failed",
                    errno=(payload or {}).get("errno"),
                )
                return None

            share_id = payload.get("share_id")
            uk = payload.get("uk")
            title = payload.get("title", "")

            items, folder_queue = [], []
            for it in payload.get("list") or []:
                if str(it.get("isdir", "0")) == "1":
                    folder_queue.append(it.get("path"))
                else:
                    items.append(it)
            visited = set()
            while folder_queue:
                dir_path = folder_queue.pop(0)
                if dir_path in visited:
                    continue
                visited.add(dir_path)
                sub = await _list(_params(short_url, 0, dir_path))
                if sub and sub.get("errno") == 0:
                    for it in sub.get("list") or []:
                        if str(it.get("isdir", "0")) == "1":
                            folder_queue.append(it.get("path"))
                        else:
                            items.append(it)

            files, incomplete = [], False
            for it in items:
                size_bytes = int(it.get("size", 0))
                tb = TeraBoxFile(
                    filename=it.get("server_filename", "unknown"),
                    size_bytes=size_bytes,
                    size_mb=round(size_bytes / (1024 * 1024), 2),
                    fs_id=str(it.get("fs_id", "")),
                    dlink=it.get("dlink"),
                    category=int(it.get("category", 0)),
                    md5=it.get("md5"),
                    thumbs=it.get("thumbs"),
                    path=it.get("path", ""),
                    server_mtime=it.get("server_mtime"),
                    backend="direct",
                )
                if tb.dlink:
                    files.append(tb)
                else:
                    incomplete = True  # needs transfer -> fall back to Method B

            if not files:
                self.logger.warning("Method A: no downloadable files found")
                return None
            if incomplete:
                # Some files lack a direct dlink; Method B transfers them to
                # account storage, guaranteeing a complete download.
                self.logger.warning(
                    "Method A incomplete: some files lack dlink -> fallback to B"
                )
                return None

            return TeraBoxResult(
                status="success",
                title=title,
                share_id=share_id,
                uk=uk,
                files=files,
                method="direct",
            )
        except Exception as e:
            self.logger.warning("Method A failed", error=str(e), exc_info=True)
            return None

    async def resolve_method_b(
        self,
        url: str,
        mode: str = "download",
        wait_for_transcoding: bool = False,
    ) -> TeraBoxResult:
        """
        Method B: resolve via the bot's own TeraBox account (legacy flow).

          1. GET /share/list — fetch file metadata (shareid, uk, file list)
          2. POST /share/transfer — copy files to account storage
          3. GET /api/filemetas — resolve direct download links
          4. (Optional) GET /api/streaming — resolve HLS stream URLs

        Returns:
            TeraBoxResult(method="account") with file info and download links
        """
        # Parse surl from URL
        try:
            surl = self.parse_surl(url)
        except TeraBoxURLError:
            raise
        except Exception as e:
            raise TeraBoxURLError(f"Failed to parse URL: {e}")

        self.logger.info("Resolving surl", surl=surl, mode=mode)

        # Ensure root directory exists (for transfer)
        if mode != "list":
            await self._ensure_root_dir()

        # Get share info (Step 1: resolve link)
        try:
            # First fetch root folder list to get share_id, uk, and title
            root_info = await self._get_share_list(surl, dir_path="/", root=1)
            share_id = root_info["share_id"]
            uk = root_info["uk"]
            title = root_info["title"]
            raw_response = root_info["raw"]
            
            # Recursively traverse all subdirectories
            file_list = []
            queue = [("/", 1)]
            while queue:
                current_dir, is_root = queue.pop(0)
                try:
                    dir_info = await self._get_share_list(surl, dir_path=current_dir, root=is_root)
                    items = dir_info.get("file_list") or []
                    for item in items:
                        file_list.append(item)
                        if int(item.get("isdir", 0)) == 1:
                            queue.append((item.get("path"), 0))
                except Exception as e:
                    self.logger.error("Failed to list directory", directory=current_dir, error=str(e))
        except TeraBoxAPIError:
            raise
        except Exception as e:
            return TeraBoxResult(
                status="error",
                error=f"Failed to get share info: {e}",
            )

        if not file_list:
            return TeraBoxResult(
                status="error",
                share_id=share_id,
                uk=uk,
                title=title,
                error="No files found in share",
            )

        # Get existing files to skip re-transfers
        existing_files = set()
        if mode != "list":
            existing_files = await self._get_existing_files()

        # Parallel file processing with concurrency limit
        semaphore = asyncio.Semaphore(5)
        async def _process_with_limit(item):
            async with semaphore:
                return await self._process_file(
                    item=item,
                    share_id=share_id,
                    uk=uk,
                    existing_files=existing_files,
                    action=mode,
                    wait_for_transcoding=wait_for_transcoding,
                )
        resolved_files = list(await asyncio.gather(*[_process_with_limit(item) for item in file_list]))

        return TeraBoxResult(
            status="success",
            title=title,
            share_id=share_id,
            uk=uk,
            files=resolved_files,
            raw_response=raw_response,
        )

    async def list_files(self, url: str) -> TeraBoxResult:
        """
        List files in a TeraBox share without downloading or transferring.

        Args:
            url: TeraBox share link

        Returns:
            TeraBoxResult with file metadata only
        """
        return await self.resolve(url, mode="list")

    async def get_download_links(self, url: str) -> TeraBoxResult:
        """
        Resolve a TeraBox share link and get direct download links.

        Args:
            url: TeraBox share link

        Returns:
            TeraBoxResult with direct download links in each file's dlink
        """
        return await self.resolve(url, mode="download")

    async def get_stream_links(
        self, url: str, wait: bool = False
    ) -> TeraBoxResult:
        """
        Resolve a TeraBox share link and get HLS stream URLs.

        Args:
            url: TeraBox share link
            wait: Whether to wait if transcoding is in progress

        Returns:
            TeraBoxResult with HLS streaming URLs
        """
        return await self.resolve(url, mode="stream", wait_for_transcoding=wait)

    async def download(
        self,
        url: str,
        output_dir: str = ".",
        chunk_size: int = 8192,
        progress_callback: Callable = None,
    ) -> List[str]:
        """
        Download all files from a TeraBox share link to disk.

        Args:
            url: TeraBox share link
            output_dir: Directory to save files (created if needed)
            chunk_size: Download chunk size in bytes
            progress_callback: Optional callable(filename, downloaded, total)
                               called during download for progress reporting

        Returns:
            List of downloaded file paths

        Raises:
            TeraBoxError: If resolution or download fails
        """
        result = await self.resolve(url, mode="download")

        if not result.ok:
            raise TeraBoxError(f"Failed to resolve: {result.error}")

        os.makedirs(output_dir, exist_ok=True)
        downloaded_paths = []

        for tb_file in result.files:
            if not tb_file.dlink:
                self.logger.warning("No download link for file", filename=tb_file.filename)
                continue

            filepath = os.path.join(output_dir, tb_file.filename)
            self.logger.info(
                "Downloading file", filename=tb_file.filename, size_mb=tb_file.size_mb
            )

            # Method A (direct share) dlinks need the impersonated curl_cffi
            # session — plain aiohttp is rejected (HTTP 403) by the CDN.
            if getattr(tb_file, "backend", "account") == "direct":
                await self._download_direct_async(tb_file, filepath)
                downloaded_paths.append(filepath)
                self.logger.info("File saved (direct)", path=filepath)
                continue

            try:
                # Download with proper User-Agent (required by TeraBox CDN)
                r = await self._request_with_retry(
                    "GET",
                    tb_file.dlink,
                    headers={"User-Agent": self.USER_AGENT},
                )
                try:
                    total = int(r.headers.get("content-length", 0))
                    downloaded = 0

                    with open(filepath, "wb") as f:
                        async for chunk in r.content.iter_chunked(chunk_size):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback:
                                progress_callback(
                                    tb_file.filename, downloaded, total
                                )

                    downloaded_paths.append(filepath)
                    self.logger.info("File saved", path=filepath)
                finally:
                    await r.release()

            except Exception as e:
                self.logger.error("Download failed", filename=tb_file.filename, error=str(e))
                raise TeraBoxError(
                    f"Download failed for {tb_file.filename}: {e}"
                )

        return downloaded_paths



    def to_dict(self, result: TeraBoxResult) -> Dict[str, Any]:
        """
        Convert a TeraBoxResult to a JSON-serializable dictionary.
        Matches the TeraBridge API response format.

        Args:
            result: TeraBoxResult to convert

        Returns:
            dict matching the API JSON response format
        """
        return {
            "status": result.status,
            "title": result.title,
            "share_id": result.share_id,
            "uk": result.uk,
            "error": result.error,
            "files": [
                {
                    "filename": f.filename,
                    "size_bytes": f.size_bytes,
                    "size_mb": f.size_mb,
                    "fs_id": f.fs_id,
                    "transfer_status": f.transfer_status,
                    "dlink": f.dlink,
                    "stream_url": f.stream_url,
                    "stream_ready": f.stream_ready,
                    "stream_m3u8": f.stream_m3u8,
                    "error": f.error,
                    "is_dir": f.is_dir,
                    "category": f.category,
                    "md5": f.md5,
                    "path": f.path,
                }
                for f in result.files
            ],
        }

    # ─── Utilities ───────────────────────────────────────────────────

    @staticmethod
    def _parse_cookies(cookie_str: str) -> Dict[str, str]:
        """Parse a cookie header string into a dict."""
        cookies = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
        return cookies

    def __repr__(self):
        ndus = self._cookies_dict.get("ndus", "")
        masked = f"{ndus[:8]}..." if len(ndus) > 8 else ndus
        tokens = "resolved" if self._tokens_resolved else "manual"
        return f"TeraBoxDownloader(ndus={masked!r}, tokens={tokens}, root={self.root_path!r})"

    async def __call__(self, url: str, mode: str = "download", **kwargs) -> TeraBoxResult:
        """
        Make the instance callable — resolves a share link directly.

        Usage:
            tb = TeraBoxDownloader(...)
            result = tb("https://terabox.com/s/1ABCDEFG...")
        """
        return await self.resolve(url, mode=mode, **kwargs)


# ─── CLI Entry Point ─────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    # Reconfigure stdout for UTF-8 on Windows
    if sys.version_info >= (3, 7):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description="TeraBox Downloader CLI")
    parser.add_argument("url", nargs="?", help="TeraBox share link")
    parser.add_argument(
        "--mode", "-m",
        choices=["download", "stream", "list"],
        default="download",
        help="Action mode (default: download)"
    )
    parser.add_argument("--cookie", "-c", help="Override TeraBox session cookie (ndus)")
    parser.add_argument("--validate", "-v", action="store_true", help="Validate current session cookie and exit")
    parser.add_argument("--download", "-d", action="store_true", help="Actually download resolved files (only valid in download mode)")
    parser.add_argument("--output-dir", "-o", default=".", help="Directory to save downloaded files (default: current directory)")

    args = parser.parse_args()

    # If no URL and not validating, print help and exit
    if not args.url and not args.validate:
        parser.print_help()
        sys.exit(1)

    # Configure logger to only output warnings/errors to prevent polluting stdout JSON
    import logging as _logging
    _logging.getLogger("TeraBoxDownloader").setLevel(_logging.WARNING)
    _logging.basicConfig(level=_logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

    async def main():
        # Action 1: Validate session cookie
        if args.validate:
            try:
                tb = TeraBoxDownloader(cookie=args.cookie) if args.cookie else TeraBoxDownloader()
                is_valid, msg = await tb.validate_session()
                print(json.dumps({
                    "valid": is_valid,
                    "message": msg
                }, indent=2))
                await tb.close()
                sys.exit(0 if is_valid else 1)
            except Exception as e:
                print(json.dumps({
                    "valid": False,
                    "error": str(e)
                }, indent=2))
                sys.exit(1)

        # Action 2: Resolve URL
        tb = TeraBoxDownloader(cookie=args.cookie) if args.cookie else TeraBoxDownloader()
        try:
            result = await tb.resolve(args.url, mode=args.mode)

            # Print results as JSON
            print(json.dumps(tb.to_dict(result), indent=2))

            if not result.ok:
                sys.exit(1)

            # Download files if requested
            if args.mode == "download" and args.download:
                sys.stderr.write("\nStarting download of resolved files...\n")

                def progress(filename, downloaded, total):
                    if total > 0:
                        pct = downloaded / total * 100
                        sys.stderr.write(f"\r  {filename}: {pct:.1f}% ({downloaded / 1024 / 1024:.2f} / {total / 1024 / 1024:.2f} MB)")
                        sys.stderr.flush()

                for tb_file in result.files:
                    if tb_file.is_dir or not tb_file.dlink or tb_file.error:
                        continue
                    filepath = os.path.join(args.output_dir, tb_file.filename)
                    sys.stderr.write(f"\nDownloading {tb_file.filename} ({tb_file.size_mb:.2f} MB)...\n")

                    os.makedirs(args.output_dir, exist_ok=True)
                    r = await tb._request_with_retry(
                        "GET",
                        tb_file.dlink,
                        headers={"User-Agent": tb.USER_AGENT},
                    )
                    try:
                        total = int(r.headers.get("content-length", 0))
                        downloaded = 0
                        with open(filepath, "wb") as f:
                            async for chunk in r.content.iter_chunked(8192):
                                f.write(chunk)
                                downloaded += len(chunk)
                                progress(tb_file.filename, downloaded, total)
                    finally:
                        await r.release()
                    sys.stderr.write("\n")

                sys.stderr.write("\n[SUCCESS] All downloads completed!\n")

        except Exception as e:
            print(json.dumps({
                "status": "error",
                "error": str(e)
            }, indent=2))
            sys.exit(1)
        finally:
            await tb.close()

    asyncio.run(main())
