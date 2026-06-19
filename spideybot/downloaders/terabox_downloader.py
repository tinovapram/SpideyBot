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

import requests
import json
import urllib.parse
import re
import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple, Callable
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
        root_path: Account folder to copy files into (default: /cloudvids)
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

    def __init__(
        self,
        cookie: str = None,
        js_token: str = None,
        bds_token: str = None,
        sign: str = None,
        timestamp: str = None,
        logid: str = None,
        root_path: str = "/cloudvids",
        timeout: int = 30,
        auto_resolve_tokens: bool = True,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        pool_connections: int = 10,
        pool_maxsize: int = 20,
    ):
        self.logger = logging.getLogger("TeraBoxDownloader")

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

        self.root_path = root_path
        self.timeout = timeout
        self._tokens_resolved = False

        # ── HTTP Headers ─────────────────────────────────────────────
        self._headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.8",
            "Referer": f"{self.BASE_API}/main?category=all&path=%2F",
            "X-Requested-With": "XMLHttpRequest",
        }

        # ── Build session with connection pooling and retry ──────────
        self.session = requests.Session()
        self.session.headers.update(self._headers)
        self.session.cookies.update(self._cookies_dict)

        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
            max_retries=retry_strategy,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # ── Auto-resolve tokens if needed ────────────────────────────
        if auto_resolve_tokens and (not self.js_token or not self.bds_token):
            try:
                self._resolve_tokens()
            except Exception as e:
                self.logger.warning(
                    f"Auto-resolve tokens failed: {e}. "
                    f"You may need to provide js_token and bds_token manually."
                )

    # ─── Token Auto-Resolution ───────────────────────────────────────

    def _resolve_tokens(self):
        """
        Scrape jsToken and bdstoken from the TeraBox main page.

        The TeraBox web app embeds these tokens in the page HTML/JS when
        you load the main drive page with a valid session cookie.
        """
        self.logger.info("Auto-resolving jsToken and bdstoken from session...")

        try:
            r = self.session.get(f"{self.BASE_API}/main", timeout=self.timeout)
            if r.status_code != 200:
                raise TeraBoxAuthError(
                    f"Main page returned HTTP {r.status_code}. Cookie may be invalid."
                )

            html = urllib.parse.unquote(r.text)

            # Extract bdstoken
            if not self.bds_token:
                m = self._BDSTOKEN_PATTERN.findall(html)
                if m:
                    self.bds_token = m[0]
                    self.logger.info(f"Resolved bdstoken: {self.bds_token[:8]}...")
                else:
                    self.logger.warning("Could not find bdstoken in page HTML")

            # Extract jsToken
            if not self.js_token:
                for pattern in self._JSTOKEN_PATTERNS:
                    m = pattern.findall(html)
                    if m:
                        self.js_token = m[0]
                        self.logger.info(f"Resolved jsToken: {self.js_token[:16]}...")
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
            self.session.cookies.clear()
            self.session.cookies.update(self._cookies_dict)
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

    def validate_session(self) -> Tuple[bool, str]:
        """
        Verify if the current cookie session is valid by checking for a
        bdstoken in the main page response.

        Returns:
            (bool, str): (is_valid, message)
        """
        try:
            r = self.session.get(f"{self.BASE_API}/main", timeout=self.timeout)
            if r.status_code != 200:
                return False, f"HTTP status {r.status_code}"

            m = self._BDSTOKEN_PATTERN.findall(r.text)
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

    def _get_share_list(
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
        self.logger.debug(f"Resolving share: GET {url}")
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()

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



    def _list_dir(self, path: str) -> List[Dict]:
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
            r = self.session.get(url, timeout=self.timeout)
            data = r.json()
            if data.get("errno") == 0:
                return data.get("list") or []
        except Exception:
            pass
        return []

    def _get_existing_files(self) -> Dict[str, Dict[str, Any]]:
        """Get dict of filenames already in the root_path folder mapped to their metadata."""
        items = self._list_dir(self.root_path)
        return {
            item.get("server_filename"): {
                "fs_id": str(item.get("fs_id", "")),
                "path": item.get("path", ""),
                "size": int(item.get("size", 0))
            }
            for item in items if item.get("server_filename")
        }

    def _ensure_root_dir(self):
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
            self.session.post(url, data=payload, timeout=self.timeout)
        except Exception:
            pass  # Directory may already exist

    def _transfer_file(
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

        self.logger.debug(f"Transferring {filename} (fs_id={fs_id})")
        r = self.session.post(url, data=payload, timeout=self.timeout)
        data = r.json()
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

    def _get_download_link(self, fs_id: str) -> Optional[str]:
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

        r = self.session.get(url, timeout=self.timeout)
        data = r.json()

        if data.get("errno") == 0:
            info = data.get("list") or data.get("info") or []
            if isinstance(info, list) and info:
                return info[0].get("dlink")

        return None

    def _get_stream_info(
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
            r = self.session.get(url, timeout=self.timeout)
            
            # If the response contains EXTM3U playlist, it means it is ready and transcoded
            if r.status_code == 200 and "#EXTM3U" in r.text:
                return {
                    "stream_url": url,
                    "stream_ready": True,
                    "stream_m3u8": r.text,
                }
            
            try:
                data = r.json()
                errno = data.get("errno", -1)
            except Exception:
                errno = -1
                data = {}

            if errno == 0:
                return {
                    "stream_url": data.get("lurl") or data.get("m3u8_url") or url,
                    "stream_ready": True,
                    "ltime": data.get("ltime"),
                }
            elif errno == 130 and wait_for_transcoding:
                # errno 130 = transcoding in progress
                self.logger.info(
                    f"Transcoding in progress (attempt {attempt + 1}/{max_attempts}), "
                    f"waiting 5s..."
                )
                time.sleep(5)
            else:
                break

        return {
            "stream_url": None,
            "stream_ready": False,
            "errno": errno,
            "error": data.get("errmsg") if data else f"Streaming error (errno {errno})"
        }

    def _process_file(
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
                transfer = self._transfer_file(share_id, uk, fs_id, filename)
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
                except Exception:
                    pass
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
                dlink = self._get_download_link(my_fs_id)
                result.dlink = dlink
            except Exception as e:
                result.error = f"Failed to get download link: {e}"

        # Resolve stream info for video files (category 1 = video)
        if action == "stream" and category in (1, 3):
            try:
                target_path = f"{self.root_path}/{filename}"
                stream_info = self._get_stream_info(
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

    # ─── Public API ──────────────────────────────────────────────────

    def resolve(
        self,
        url: str,
        mode: str = "download",
        wait_for_transcoding: bool = False,
    ) -> TeraBoxResult:
        """
        Resolve a TeraBox share link into download/stream URLs.

        This is the main entry point. The flow is:
          1. Parse the share URL to extract the surl
          2. GET /share/list — fetch file metadata (shareid, uk, file list)
          3. POST /share/transfer — copy files to account storage
          4. GET /rest/2.0/pcs/file — resolve direct download links
          5. (Optional) GET /api/streaming — resolve HLS stream URLs

        Args:
            url: TeraBox share link (any recognized format)
            mode: "download" (direct links), "stream" (HLS manifest),
                  or "list" (metadata only, no transfer)
            wait_for_transcoding: If True and mode="stream", blocks and
                                  retries when transcoding is in progress

        Returns:
            TeraBoxResult with file info and download links

        Raises:
            TeraBoxURLError: If the URL is invalid
            TeraBoxAPIError: If the TeraBox API returns an error
        """
        # Parse surl from URL
        try:
            surl = self.parse_surl(url)
        except TeraBoxURLError:
            raise
        except Exception as e:
            raise TeraBoxURLError(f"Failed to parse URL: {e}")

        self.logger.info(f"Resolving surl={surl}, mode={mode}")

        # Ensure root directory exists (for transfer)
        if mode != "list":
            self._ensure_root_dir()

        # Get share info (Step 1: resolve link)
        try:
            # First fetch root folder list to get share_id, uk, and title
            root_info = self._get_share_list(surl, dir_path="/", root=1)
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
                    dir_info = self._get_share_list(surl, dir_path=current_dir, root=is_root)
                    items = dir_info.get("file_list") or []
                    for item in items:
                        file_list.append(item)
                        if int(item.get("isdir", 0)) == 1:
                            queue.append((item.get("path"), 0))
                except Exception as e:
                    self.logger.error(f"Failed to list directory {current_dir}: {e}")
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
            existing_files = self._get_existing_files()

        # Process each file
        resolved_files = []
        for item in file_list:
            tb_file = self._process_file(
                item=item,
                share_id=share_id,
                uk=uk,
                existing_files=existing_files,
                action=mode,
                wait_for_transcoding=wait_for_transcoding,
            )
            resolved_files.append(tb_file)

        return TeraBoxResult(
            status="success",
            title=title,
            share_id=share_id,
            uk=uk,
            files=resolved_files,
            raw_response=raw_response,
        )

    def list_files(self, url: str) -> TeraBoxResult:
        """
        List files in a TeraBox share without downloading or transferring.

        Args:
            url: TeraBox share link

        Returns:
            TeraBoxResult with file metadata only
        """
        return self.resolve(url, mode="list")

    def get_download_links(self, url: str) -> TeraBoxResult:
        """
        Resolve a TeraBox share link and get direct download links.

        Args:
            url: TeraBox share link

        Returns:
            TeraBoxResult with direct download links in each file's dlink
        """
        return self.resolve(url, mode="download")

    def get_stream_links(
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
        return self.resolve(url, mode="stream", wait_for_transcoding=wait)

    def download(
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
        result = self.resolve(url, mode="download")

        if not result.ok:
            raise TeraBoxError(f"Failed to resolve: {result.error}")

        os.makedirs(output_dir, exist_ok=True)
        downloaded_paths = []

        for tb_file in result.files:
            if not tb_file.dlink:
                self.logger.warning(f"No download link for {tb_file.filename}")
                continue

            filepath = os.path.join(output_dir, tb_file.filename)
            self.logger.info(
                f"Downloading {tb_file.filename} ({tb_file.size_mb:.2f} MB)..."
            )

            try:
                # Download with proper User-Agent (required by TeraBox CDN)
                r = self.session.get(
                    tb_file.dlink,
                    stream=True,
                    timeout=self.timeout,
                    headers={"User-Agent": self.USER_AGENT},
                )
                r.raise_for_status()

                total = int(r.headers.get("content-length", 0))
                downloaded = 0

                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback:
                                progress_callback(
                                    tb_file.filename, downloaded, total
                                )

                downloaded_paths.append(filepath)
                self.logger.info(f"Saved: {filepath}")

            except Exception as e:
                self.logger.error(f"Download failed for {tb_file.filename}: {e}")
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

    def __call__(self, url: str, mode: str = "download", **kwargs) -> TeraBoxResult:
        """
        Make the instance callable — resolves a share link directly.

        Usage:
            tb = TeraBoxDownloader(...)
            result = tb("https://terabox.com/s/1ABCDEFG...")
        """
        return self.resolve(url, mode=mode, **kwargs)


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
    logging.getLogger("TeraBoxDownloader").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

    # Action 1: Validate session cookie
    if args.validate:
        try:
            tb = TeraBoxDownloader(cookie=args.cookie) if args.cookie else TeraBoxDownloader()
            is_valid, msg = tb.validate_session()
            print(json.dumps({
                "valid": is_valid,
                "message": msg
            }, indent=2))
            sys.exit(0 if is_valid else 1)
        except Exception as e:
            print(json.dumps({
                "valid": False,
                "error": str(e)
            }, indent=2))
            sys.exit(1)

    # Action 2: Resolve URL
    try:
        tb = TeraBoxDownloader(cookie=args.cookie) if args.cookie else TeraBoxDownloader()
        result = tb.resolve(args.url, mode=args.mode)
        
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
                r = tb.session.get(
                    tb_file.dlink,
                    stream=True,
                    timeout=tb.timeout,
                    headers={"User-Agent": tb.USER_AGENT},
                )
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            progress(tb_file.filename, downloaded, total)
                sys.stderr.write("\n")
            
            sys.stderr.write("\n[SUCCESS] All downloads completed!\n")
            
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "error": str(e)
        }, indent=2))
        sys.exit(1)
