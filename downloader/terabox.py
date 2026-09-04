"""
TeraBox link resolver and downloader.

Resolves TeraBox share links into direct download URLs using two distinct
strategies (each in its own class):

- ``DirectShareResolver`` (**Method A**) — impersonate desktop Chrome via
  ``curl_cffi`` and download straight from the share CDN (no account storage).
- ``AccountTransferResolver`` (**Method B**) — copy files into the bot's own
  TeraBox account, then resolve per-file download links via the account API.

``TeraBoxDownloader`` is a small facade that composes both and exposes the
public API.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

import aiohttp
import structlog

from core import config
from downloader.terabox_transfer import (
    aria2_available,
    aria2_download,
    pick_transfer_backend,
    segmented_download,
    single_stream_download,
    wipe_partial,
)
from utils.files import sanitize_filename

try:
    from curl_cffi import requests as _cffi_requests
    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover
    _cffi_requests = None
    HAS_CURL_CFFI = False


# ── Data classes ───────────────────────────────────────────────────

@dataclass
class TeraBoxFile:
    """A single file resolved from a TeraBox share link."""

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
    thumbs: Optional[dict] = None
    path: Optional[str] = None
    server_mtime: Optional[int] = None
    backend: str = "account"  # "account" (Method B) or "direct" (Method A)

    def __repr__(self) -> str:
        return f"TeraBoxFile([{self.transfer_status}] {self.filename!r}, {self.size_mb:.2f} MB)"


@dataclass
class TeraBoxResult:
    """Result of resolving a TeraBox share link."""

    status: str
    title: Optional[str] = None
    share_id: Optional[int] = None
    uk: Optional[int] = None
    files: list[TeraBoxFile] = field(default_factory=list)
    error: Optional[str] = None
    raw_response: Optional[dict] = None
    method: str = "account"

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def download_links(self) -> list[str]:
        return [f.dlink for f in self.files if f.dlink]

    def __repr__(self) -> str:
        if self.ok:
            return f"TeraBoxResult(success, {len(self.files)} file(s), title={self.title!r})"
        return f"TeraBoxResult({self.status}, error={self.error!r})"


# ── Exceptions ─────────────────────────────────────────────────────

class TeraBoxError(Exception):
    """Base exception for TeraBox errors."""


class TeraBoxAuthError(TeraBoxError):
    """Authentication/session is invalid or tokens cannot be resolved."""


class TeraBoxURLError(TeraBoxError):
    """Share URL is invalid or cannot be parsed."""


class TeraBoxAPIError(TeraBoxError):
    """The TeraBox API returned an error."""

    def __init__(self, message: str, errno: Optional[int] = None) -> None:
        super().__init__(message)
        self.errno = errno


# ── Shared helpers ─────────────────────────────────────────────────

_SURL_MIN_LEN = 8
_LEADING_ONE_MAX_STRIPS = 4
_VALID_SURL = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_surl(url: str) -> str:
    """Extract and clean the shorturl key (surl) from a TeraBox share link."""
    if not isinstance(url, str) or not url:
        raise TeraBoxURLError("Empty or non-string input")

    surl = None
    if "/api/shorturlinfo?" in url:
        surl = url.split("/api/shorturlinfo?", 1)[1].split("&", 1)[0].split("#", 1)[0].strip()
    elif "surl=" in url:
        surl = url.split("surl=", 1)[1].split("&", 1)[0]
    elif "/s/" in url:
        surl = url.split("/s/", 1)[1].split("?", 1)[0].split("#", 1)[0]
    else:
        stripped = url.strip()
        if "://" in stripped or "/" in stripped or "." in stripped:
            raise TeraBoxURLError(f"No surl marker found in {url!r}")
        if _VALID_SURL.match(stripped) and len(stripped) >= _SURL_MIN_LEN:
            surl = stripped

    if not surl:
        raise TeraBoxURLError(f"No surl found in {url!r}")

    surl = surl.rstrip("/").split("/")[-1]
    if not _VALID_SURL.match(surl):
        raise TeraBoxURLError(f"Extracted value {surl!r} contains invalid characters")

    if len(surl) > 22 and surl.startswith("1"):
        for _ in range(_LEADING_ONE_MAX_STRIPS):
            if not surl.startswith("1") or len(surl) - 1 < _SURL_MIN_LEN or len(surl) <= 22:
                break
            surl = surl[1:]

    if len(surl) < _SURL_MIN_LEN:
        raise TeraBoxURLError(f"Cleaned surl {surl!r} is too short")

    return surl


def parse_cookies(cookie_str: str) -> dict[str, str]:
    """Parse a cookie header string into a dict."""
    cookies: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


# ── Method B: account transfer resolver ────────────────────────────

class AccountTransferResolver:
    """Resolve links via the bot's own TeraBox account (Method B)."""

    BASE_API = "https://dm.terabox.com"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )

    _JSTOKEN_PATTERNS = [
        re.compile(r'jsToken["\']?\s*[:=]\s*["\']([A-F0-9]{64,})["\']', re.IGNORECASE),
        re.compile(r'fn\("([A-F0-9]{64,})"\)', re.IGNORECASE),
    ]
    _BDSTOKEN_PATTERN = re.compile(r'bdstoken["\']?\s*[:=]\s*["\']([a-f0-9]{32})["\']', re.IGNORECASE)
    _LOGID_PATTERN = re.compile(r'logid["\']?\s*[:=]\s*["\'](\d{15,})["\']', re.IGNORECASE)
    _SIGN_PATTERN = re.compile(r'sign["\']?\s*[:=]\s*["\']([A-Za-z0-9%+=/_-]{20,})["\']', re.IGNORECASE)
    _TIMESTAMP_PATTERN = re.compile(r'timestamp["\']?\s*[:=]\s*["\']?(\d{10})["\']?', re.IGNORECASE)

    parse_surl = staticmethod(parse_surl)

    def __init__(
        self,
        cookie: str,
        js_token: str = "",
        bds_token: str = "",
        sign: str = "",
        timestamp: str = "",
        logid: str = "",
        root_path: str = "/downloads",
        timeout: int = 30,
        auto_resolve_tokens: bool = True,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        pool_maxsize: int = 20,
    ) -> None:
        self.logger = structlog.get_logger("AccountTransferResolver")
        self.cookie = cookie
        self._cookies_dict = parse_cookies(cookie)
        self.js_token = js_token
        self.bds_token = bds_token
        self.sign = sign
        self.timestamp = timestamp
        self.logid = logid
        self.root_path = root_path
        self.timeout = timeout
        self._tokens_resolved = False
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._pool_maxsize = pool_maxsize
        self._auto_resolve_tokens = auto_resolve_tokens

        self._headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.8",
            "Referer": f"{self.BASE_API}/main?category=all&path=%2F",
            "X-Requested-With": "XMLHttpRequest",
        }
        self.session: Optional[aiohttp.ClientSession] = None

    # ── Session lifecycle ──────────────────────────────────────────

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=10, limit_per_host=self._pool_maxsize, enable_cleanup_closed=True
            )
            self.session = aiohttp.ClientSession(
                headers=self._headers,
                cookies=self._cookies_dict,
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
            if self._auto_resolve_tokens and (not self.js_token or not self.bds_token):
                try:
                    await self._resolve_tokens()
                except Exception as exc:
                    self.logger.warning("Auto-resolve tokens failed", error=str(exc))
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        session = await self._ensure_session()
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                response = await session.request(method, url, **kwargs)
                if response.status in (500, 502, 503, 504) and attempt < self._max_retries - 1:
                    await response.release()
                    await asyncio.sleep(self._backoff_factor * (2 ** attempt))
                    continue
                return response
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._backoff_factor * (2 ** attempt))
                    continue
                raise
        raise last_exc or RuntimeError("Request failed after retries")

    # ── Token auto-resolution ──────────────────────────────────────

    async def _resolve_tokens(self) -> None:
        response = await self._request_with_retry("GET", f"{self.BASE_API}/main")
        if response.status != 200:
            await response.release()
            raise TeraBoxAuthError(f"Main page returned HTTP {response.status}.")

        html = urllib.parse.unquote(await response.text())
        await response.release()

        if not self.bds_token:
            match = self._BDSTOKEN_PATTERN.findall(html)
            if match:
                self.bds_token = match[0]
        if not self.js_token:
            for pattern in self._JSTOKEN_PATTERNS:
                match = pattern.findall(html)
                if match:
                    self.js_token = match[0]
                    break
        if not self.logid:
            match = self._LOGID_PATTERN.findall(html)
            if match:
                self.logid = match[0]
        if not self.sign:
            match = self._SIGN_PATTERN.findall(html)
            if match:
                self.sign = match[0]
        if not self.timestamp:
            match = self._TIMESTAMP_PATTERN.findall(html)
            if match:
                self.timestamp = match[0]

        self._tokens_resolved = True
        self.logger.info("Token resolution complete")

    # ── Credentials ────────────────────────────────────────────────

    def update_credentials(
        self,
        cookie: Optional[str] = None,
        js_token: Optional[str] = None,
        bds_token: Optional[str] = None,
        sign: Optional[str] = None,
        timestamp: Optional[str] = None,
        logid: Optional[str] = None,
    ) -> None:
        if cookie:
            self.cookie = cookie
            self._cookies_dict = parse_cookies(cookie)
            self.session = None
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

    async def validate_session(self) -> tuple[bool, str]:
        try:
            response = await self._request_with_retry("GET", f"{self.BASE_API}/main")
            if response.status != 200:
                await response.release()
                return False, f"HTTP status {response.status}"
            text = await response.text()
            await response.release()
            if self._BDSTOKEN_PATTERN.findall(text):
                return True, "Valid"
            return False, "bdstoken not found (session likely expired)"
        except Exception as exc:
            return False, f"Request failed: {exc}"

    # ── API helpers (Method B) ─────────────────────────────────────

    def _query_params(self) -> str:
        params = "app_id=250528&web=1&channel=dubox&clienttype=0"
        if self.js_token:
            params += f"&jsToken={self.js_token}"
        if self.logid:
            params += f"&dp-logid={self.logid}"
        return params

    async def _get_share_list(self, surl: str, dir_path: str = "/", root: int = 1) -> dict:
        url = (
            f"{self.BASE_API}/share/list?{self._query_params()}"
            f"&shorturl={surl}&root={root}&dir={urllib.parse.quote(dir_path)}"
            f"&order=time&desc=1&num=100&page=1"
        )
        response = await self._request_with_retry("GET", url)
        try:
            data = await response.json()
        finally:
            await response.release()

        errno = data.get("errno", -1)
        if errno != 0:
            raise TeraBoxAPIError(
                f"share/list failed: errno={errno}, errmsg={data.get('errmsg', 'Unknown error')}",
                errno=errno,
            )

        return {
            "share_id": data.get("shareid") or data.get("share_id"),
            "uk": data.get("uk"),
            "title": data.get("title", ""),
            "file_list": data.get("list") or data.get("file_list") or [],
            "raw": data,
        }

    async def _list_dir(self, path: str) -> list:
        url = (
            f"{self.BASE_API}/api/list?{self._query_params()}"
            f"&dir={urllib.parse.quote(path)}&order=time&desc=1&num=1000"
        )
        try:
            response = await self._request_with_retry("GET", url)
            try:
                data = await response.json()
            finally:
                await response.release()
            if data.get("errno") == 0:
                return data.get("list") or []
        except Exception as exc:
            self.logger.warning("_list_dir request failed", path=path, error=str(exc))
        return []

    async def _get_existing_files(self) -> dict[str, dict]:
        items = await self._list_dir(self.root_path)
        return {
            item["server_filename"]: {
                "fs_id": str(item.get("fs_id", "")),
                "path": item.get("path", ""),
                "size": int(item.get("size", 0)),
            }
            for item in items
            if item.get("server_filename")
        }

    async def _ensure_root_dir(self) -> None:
        url = f"{self.BASE_API}/api/create?{self._query_params()}&bdstoken={self.bds_token}"
        payload = {"path": self.root_path, "isdir": "1", "block_list": "[]"}
        try:
            response = await self._request_with_retry("POST", url, data=payload)
            await response.release()
        except Exception as exc:
            self.logger.warning("Failed to create root directory", path=self.root_path, error=str(exc))

    async def _transfer_file(self, share_id: int, uk: int, fs_id: str, filename: str) -> dict:
        url = (
            f"{self.BASE_API}/share/transfer?{self._query_params()}"
            f"&shareid={share_id}&from={uk}&bdstoken={self.bds_token}"
        )
        payload = {"fsidlist": f"[{fs_id}]", "path": self.root_path}

        response = await self._request_with_retry("POST", url, data=payload)
        try:
            data = await response.json()
        finally:
            await response.release()

        errno = data.get("errno", -1)
        if errno == 0:
            status = "success"
        elif errno in (12, -33):
            status = "already_exists"
        else:
            status = "error"

        return {
            "status": status,
            "errno": errno,
            "raw": data,
            "error": None if status != "error" else (
                data.get("errmsg") or data.get("show_msg") or f"Transfer errno={errno}"
            ),
        }

    async def _get_download_link(self, fs_id: str) -> Optional[str]:
        fsids = urllib.parse.quote(json.dumps([str(fs_id)]))
        url = (
            f"{self.BASE_API}/api/filemetas?{self._query_params()}"
            f"&fsids={fsids}&dlink=1&thumb=0&bdstoken={self.bds_token}"
        )
        response = await self._request_with_retry("GET", url)
        try:
            data = await response.json()
        finally:
            await response.release()

        if data.get("errno") == 0:
            info = data.get("list") or data.get("info") or []
            if isinstance(info, list) and info:
                return info[0].get("dlink")
        return None

    async def _get_stream_info(
        self, path: str, wait_for_transcoding: bool = False, quality: str = "M3U8_AUTO_480"
    ) -> dict:
        url = (
            f"{self.BASE_API}/api/streaming?{self._query_params()}"
            f"&path={urllib.parse.quote(path)}&type={quality}&bdstoken={self.bds_token}"
        )

        attempts = 10 if wait_for_transcoding else 1
        data: dict = {}
        errno = -1
        for _ in range(attempts):
            response = await self._request_with_retry("GET", url)
            try:
                text = await response.text()
                if response.status == 200 and "#EXTM3U" in text:
                    return {"stream_url": url, "stream_ready": True, "stream_m3u8": text}
                try:
                    data = await response.json()
                except Exception:
                    data = {}
                errno = data.get("errno", -1)
            finally:
                await response.release()

            if errno == 0:
                return {
                    "stream_url": data.get("lurl") or data.get("m3u8_url") or url,
                    "stream_ready": True,
                    "ltime": data.get("ltime"),
                }
            if errno == 130 and wait_for_transcoding:
                await asyncio.sleep(5)
            else:
                break

        return {"stream_url": None, "stream_ready": False, "errno": errno}

    async def _process_file(
        self,
        item: dict,
        share_id: int,
        uk: int,
        existing_files: dict,
        action: str = "download",
        wait_for_transcoding: bool = False,
    ) -> TeraBoxFile:
        filename = item.get("server_filename", "unknown")
        fs_id = str(item.get("fs_id", ""))
        size_bytes = int(item.get("size", 0))
        is_dir = int(item.get("isdir", 0)) == 1

        result = TeraBoxFile(
            filename=filename,
            size_bytes=size_bytes,
            size_mb=round(size_bytes / (1024 * 1024), 2),
            fs_id=fs_id,
            is_dir=is_dir,
            category=int(item.get("category", 0)),
            md5=item.get("md5"),
            thumbs=item.get("thumbs"),
            path=item.get("path", ""),
            server_mtime=item.get("server_mtime"),
        )

        if action == "list":
            result.transfer_status = "listed"
            return result
        if is_dir:
            result.transfer_status = "skipped_directory"
            return result

        my_fs_id = fs_id
        try:
            if filename not in existing_files:
                transfer = await self._transfer_file(share_id, uk, fs_id, filename)
                result.transfer_status = transfer["status"]
                if transfer["status"] == "error":
                    result.error = transfer.get("error")
                    return result
                extra = transfer.get("raw", {}).get("extra", {}).get("list", [])
                if extra and extra[0].get("to_fs_id"):
                    my_fs_id = str(extra[0]["to_fs_id"])
            else:
                result.transfer_status = "already_exists"
                my_fs_id = existing_files[filename].get("fs_id") or fs_id
        except Exception as exc:
            result.transfer_status = "transfer_error"
            result.error = str(exc)
            return result

        if action in ("download", "stream"):
            try:
                result.dlink = await self._get_download_link(my_fs_id)
            except Exception as exc:
                result.error = f"Failed to get download link: {exc}"

        if action == "stream" and result.category in (1, 3):
            try:
                stream_info = await self._get_stream_info(
                    f"{self.root_path}/{filename}", wait_for_transcoding=wait_for_transcoding
                )
                result.stream_url = stream_info.get("stream_url")
                result.stream_ready = stream_info.get("stream_ready", False)
                result.stream_m3u8 = stream_info.get("stream_m3u8")
            except Exception as exc:
                result.error = f"Failed to get stream info: {exc}"

        if not result.error:
            result.transfer_status = "success"
        return result

    async def resolve(
        self, url: str, mode: str = "download", wait_for_transcoding: bool = False
    ) -> TeraBoxResult:
        try:
            surl = parse_surl(url)
        except TeraBoxURLError:
            raise
        except Exception as exc:
            raise TeraBoxURLError(f"Failed to parse URL: {exc}") from exc

        if mode != "list":
            await self._ensure_root_dir()

        root_info = await self._get_share_list(surl, dir_path="/", root=1)
        share_id = root_info["share_id"]
        uk = root_info["uk"]
        title = root_info["title"]
        raw_response = root_info["raw"]

        file_list: list = []
        queue = [("/", 1)]
        while queue:
            current_dir, is_root = queue.pop(0)
            try:
                dir_info = await self._get_share_list(surl, dir_path=current_dir, root=is_root)
                for item in dir_info.get("file_list") or []:
                    file_list.append(item)
                    if int(item.get("isdir", 0)) == 1:
                        queue.append((item.get("path"), 0))
            except Exception as exc:
                self.logger.error("Failed to list directory", directory=current_dir, error=str(exc))

        if not file_list:
            return TeraBoxResult(
                status="error", share_id=share_id, uk=uk, title=title, error="No files found in share"
            )

        existing_files = await self._get_existing_files() if mode != "list" else {}

        semaphore = asyncio.Semaphore(5)

        async def _process(item):
            async with semaphore:
                return await self._process_file(
                    item=item,
                    share_id=share_id,
                    uk=uk,
                    existing_files=existing_files,
                    action=mode,
                    wait_for_transcoding=wait_for_transcoding,
                )

        resolved = list(await asyncio.gather(*[_process(item) for item in file_list]))

        return TeraBoxResult(
            status="success", title=title, share_id=share_id, uk=uk,
            files=resolved, raw_response=raw_response,
        )


# ── Method A: direct share resolver ────────────────────────────────

class DirectShareResolver:
    """Resolve links straight from the share CDN (Method A, XDOWNDER-style)."""

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
    _DIRECT_IMPERSONATE_TARGETS = ("chrome110", "chrome120", "chrome124", "chrome131", "chrome")
    _SHARE_JSTOKEN_PATTERNS = [
        re.compile(r'fn%28%22(.*?)%22%29'),
        re.compile(r'fn\"([^\"]+)\"\)'),
        re.compile(r'jsToken\s*=\s*["\']([^"\']+)["\']'),
        re.compile(r'jsToken["\']?\s*:\s*["\']([^"\']+)["\']'),
        re.compile(r'window\.jsToken\s*=\s*["\']([^"\']+)["\']'),
    ]

    parse_surl = staticmethod(parse_surl)

    def __init__(self, cookie: str) -> None:
        self.logger = structlog.get_logger("DirectShareResolver")
        self._cookies_dict = parse_cookies(cookie)
        self._session = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _extract_share_jstoken(html_text: str) -> Optional[str]:
        if not html_text:
            return None
        for pattern in DirectShareResolver._SHARE_JSTOKEN_PATTERNS:
            match = pattern.search(html_text)
            if match and match.group(1):
                return match.group(1)
        return None

    async def _ensure_session(self):
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is not None:
                return self._session
            if not HAS_CURL_CFFI:
                raise TeraBoxAuthError("curl_cffi is required for direct (XDOWNDER) resolution")

            last_err = None
            for target in self._DIRECT_IMPERSONATE_TARGETS:
                try:
                    self._session = _cffi_requests.AsyncSession(
                        impersonate=target, cookies=dict(self._cookies_dict)
                    )
                    return self._session
                except Exception as exc:
                    last_err = exc
                    self._session = None
            raise TeraBoxAuthError(f"Could not create curl_cffi session: {last_err}")

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def download(
        self, tb_file: TeraBoxFile, filepath: str, progress_callback=None
    ) -> str:
        """Download a Method-A (route=share) dlink via curl_cffi impersonation."""
        session = await self._ensure_session()
        headers = {
            "User-Agent": self._DIRECT_UA,
            "Referer": "https://www.terabox.app/",
            "Accept": "*/*",
        }

        size_mb = (tb_file.size_bytes or 0) / (1024 * 1024)
        if size_mb < 50:
            stall_timeout = 120
        elif size_mb < 500:
            stall_timeout = 300
        elif size_mb < 2048:
            stall_timeout = 600
        else:
            stall_timeout = 900

        try:
            async with session.stream("GET", tb_file.dlink, headers=headers, timeout=3600) as response:
                if response.status_code != 200:
                    raise TeraBoxError(f"Direct download HTTP {response.status_code} for {tb_file.filename}")

                total = int(response.headers.get("Content-Length", 0) or 0)
                done = 0
                with open(filepath, "wb") as handle:
                    iterator = response.aiter_content()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=stall_timeout)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as exc:
                            raise TeraBoxError(
                                f"Download stalled for {tb_file.filename}: no data in {stall_timeout}s"
                            ) from exc
                        if chunk:
                            handle.write(chunk)
                            done += len(chunk)
                            if progress_callback:
                                progress_callback(tb_file.filename, done, total)
        except Exception:
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            raise
        return filepath

    async def resolve(self, url: str) -> Optional[TeraBoxResult]:
        """Resolve a share link directly, or return None to fall back to Method B."""
        if not HAS_CURL_CFFI:
            return None

        try:
            short_url = parse_surl(url)
        except Exception as exc:
            self.logger.warning("Method A: surl parse failed", error=str(exc))
            return None

        surl_param = short_url if short_url.startswith("1") else "1" + short_url

        try:
            session = await self._ensure_session()

            token = None
            page_headers = {
                "User-Agent": self._DIRECT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
            for host in self._DIRECT_BASE_HOSTS:
                try:
                    page_url = f"https://{host}/sharing/link?surl={surl_param}"
                    response = await session.get(page_url, headers=page_headers, timeout=15)
                    if response.status_code == 200:
                        token = self._extract_share_jstoken(response.text)
                        if token:
                            break
                except Exception as exc:
                    self.logger.debug("Method A sharing page failed", host=host, error=str(exc))

            if not token:
                return None

            api_headers = {
                "Host": self._DIRECT_API_HOST,
                "User-Agent": self._DIRECT_UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"https://{self._DIRECT_API_HOST}",
                "Referer": f"https://{self._DIRECT_API_HOST}/sharing/link?surl={short_url}&clearCache=1",
            }
            api_url = f"https://{self._DIRECT_API_HOST}/share/list"

            def _params(surl: str, root: int, dir_path: Optional[str] = None) -> dict:
                params = {
                    "app_id": "250528",
                    "jsToken": token,
                    "site_referer": "https://www.terabox.app/",
                    "shorturl": surl,
                    "root": str(root),
                }
                if dir_path is not None:
                    params["dir"] = dir_path
                return params

            async def _list(params: dict):
                response = await session.get(api_url, params=params, headers=api_headers, timeout=20)
                try:
                    return response.json()
                except Exception:
                    return None

            payload = None
            for candidate in (short_url, surl_param):
                payload = await _list(_params(candidate, 1))
                if payload and payload.get("errno") == 0:
                    break
            if not payload or payload.get("errno") != 0:
                return None

            share_id = payload.get("share_id")
            uk = payload.get("uk")
            title = payload.get("title", "")

            items, folder_queue, visited = [], [], set()
            for item in payload.get("list") or []:
                if str(item.get("isdir", "0")) == "1":
                    folder_queue.append(item.get("path"))
                else:
                    items.append(item)

            while folder_queue:
                dir_path = folder_queue.pop(0)
                if dir_path in visited:
                    continue
                visited.add(dir_path)
                sub = await _list(_params(short_url, 0, dir_path))
                if sub and sub.get("errno") == 0:
                    for item in sub.get("list") or []:
                        if str(item.get("isdir", "0")) == "1":
                            folder_queue.append(item.get("path"))
                        else:
                            items.append(item)

            files, incomplete = [], False
            for item in items:
                size_bytes = int(item.get("size", 0))
                tb = TeraBoxFile(
                    filename=item.get("server_filename", "unknown"),
                    size_bytes=size_bytes,
                    size_mb=round(size_bytes / (1024 * 1024), 2),
                    fs_id=str(item.get("fs_id", "")),
                    dlink=item.get("dlink"),
                    category=int(item.get("category", 0)),
                    md5=item.get("md5"),
                    thumbs=item.get("thumbs"),
                    path=item.get("path", ""),
                    server_mtime=item.get("server_mtime"),
                    backend="direct",
                )
                if tb.dlink:
                    files.append(tb)
                else:
                    incomplete = True

            if not files or incomplete:
                return None

            return TeraBoxResult(
                status="success", title=title, share_id=share_id, uk=uk, files=files, method="direct"
            )
        except Exception as exc:
            self.logger.warning("Method A failed", error=str(exc))
            return None


# ── Transfer helpers ───────────────────────────────────────────────

def _stall_timeout_for(size_bytes: int) -> int:
    """Pick an idle/stall timeout (seconds) proportional to file size."""
    size_mb = (size_bytes or 0) / (1024 * 1024)
    if size_mb < 50:
        return 120
    if size_mb < 500:
        return 300
    if size_mb < 2048:
        return 600
    return 900


def _transfer_headers(account: AccountTransferResolver, tb_file: TeraBoxFile) -> dict:
    """Build HTTP headers used when downloading *tb_file*'s dlink."""
    if tb_file.backend == "direct":
        ua = DirectShareResolver._DIRECT_UA
        referer = "https://www.terabox.app/"
    else:
        ua = account.USER_AGENT
        referer = f"{account.BASE_API}/main?category=all&path=%2F"
    headers = {"User-Agent": ua, "Referer": referer, "Accept": "*/*"}
    cookies = account._cookies_dict
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return headers


# ── Facade ─────────────────────────────────────────────────────────

class TeraBoxDownloader:
    """Facade composing the two resolution strategies.

    Tries the direct share resolver (Method A) first for ``download`` mode,
    then falls back to the account transfer resolver (Method B).
    """

    parse_surl = staticmethod(parse_surl)

    def __init__(
        self,
        cookie: Optional[str] = None,
        js_token: Optional[str] = None,
        bds_token: Optional[str] = None,
        sign: Optional[str] = None,
        timestamp: Optional[str] = None,
        logid: Optional[str] = None,
        root_path: Optional[str] = None,
        timeout: int = 30,
        auto_resolve_tokens: bool = True,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        pool_maxsize: int = 20,
    ) -> None:
        self.logger = structlog.get_logger("TeraBoxDownloader")
        self.cookie = cookie or os.environ.get("TERABOX_COOKIE", "")
        if not self.cookie:
            raise TeraBoxAuthError(
                "No cookie provided. Pass cookie= or set TERABOX_COOKIE env var."
            )

        self.account = AccountTransferResolver(
            cookie=self.cookie,
            js_token=js_token or os.environ.get("TERABOX_JSTOKEN", ""),
            bds_token=bds_token or os.environ.get("TERABOX_BDSTOKEN", ""),
            sign=sign or os.environ.get("TERABOX_SIGN", ""),
            timestamp=timestamp or os.environ.get("TERABOX_TIMESTAMP", ""),
            logid=logid or os.environ.get("TERABOX_LOGID", ""),
            root_path=root_path or "/downloads",
            timeout=timeout,
            auto_resolve_tokens=auto_resolve_tokens,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            pool_maxsize=pool_maxsize,
        )
        self.direct = DirectShareResolver(cookie=self.cookie)

    @property
    def root_path(self) -> str:
        return self.account.root_path

    @root_path.setter
    def root_path(self, value: str) -> None:
        self.account.root_path = value

    async def close(self) -> None:
        await self.account.close()
        await self.direct.close()

    async def __aenter__(self):
        await self.account._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def resolve(
        self, url: str, mode: str = "download", wait_for_transcoding: bool = False
    ) -> TeraBoxResult:
        if mode == "download":
            result = await self.direct.resolve(url)
            if result is not None and result.ok:
                return result
        return await self.account.resolve(url, mode=mode, wait_for_transcoding=wait_for_transcoding)

    async def download_file(
        self, tb_file: TeraBoxFile, output_dir: str, progress_callback=None
    ) -> str:
        """Download a single file using the configured transfer backend.

        Backend selection is driven by :func:`pick_transfer_backend`
        (``TERABOX_TRANSFER``). ``auto`` (the default) uses native aiohttp
        segmented download for large files, with ``aria2c`` as a second
        fallback and single-stream as the final safety net. Direct (Method A)
        links always download single-stream via curl_cffi. A failed fast
        backend never hard-fails a large file - it drops to the next backend.
        """
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, sanitize_filename(tb_file.filename))
        size_bytes = tb_file.size_bytes or 0
        headers = _transfer_headers(self.account, tb_file)
        stall = _stall_timeout_for(size_bytes)
        mode = pick_transfer_backend(size_bytes)
        log = self.logger

        try:
            # Explicit aria2 mode: always start with aria2c.
            if mode == "aria2" and aria2_available():
                try:
                    return await aria2_download(
                        tb_file.dlink,
                        filepath,
                        headers=headers,
                        expected_size=size_bytes,
                        progress_callback=progress_callback,
                        connections=config.TERABOX_ARIA2_CONNECTIONS,
                        stall_timeout=stall,
                        logger=log,
                    )
                except Exception as exc:
                    log.warning(
                        "aria2 backend failed; falling back",
                        file=tb_file.filename,
                        error=str(exc),
                    )
                    wipe_partial(filepath)

            # Segmented mode (auto default for large account links).
            if mode == "segmented" and tb_file.backend == "account":
                session = await self.account._ensure_session()
                try:
                    return await segmented_download(
                        tb_file.dlink,
                        filepath,
                        session=session,
                        headers=headers,
                        expected_size=size_bytes,
                        progress_callback=progress_callback,
                        connections=config.TERABOX_SEGMENT_CONNECTIONS,
                        stall_timeout=stall,
                        logger=log,
                    )
                except Exception as exc:
                    log.warning(
                        "segmented backend failed; trying aria2",
                        file=tb_file.filename,
                        error=str(exc),
                    )
                    wipe_partial(filepath)
                    # aria2 as second fallback before the single-stream net.
                    if aria2_available():
                        try:
                            return await aria2_download(
                                tb_file.dlink,
                                filepath,
                                headers=headers,
                                expected_size=size_bytes,
                                progress_callback=progress_callback,
                                connections=config.TERABOX_ARIA2_CONNECTIONS,
                                stall_timeout=stall,
                                logger=log,
                            )
                        except Exception as exc2:
                            log.warning(
                                "aria2 fallback failed; using single-stream",
                                file=tb_file.filename,
                                error=str(exc2),
                            )
                            wipe_partial(filepath)

            # Final fallback: original single-stream behaviour.
            wipe_partial(filepath)
            if tb_file.backend == "direct":
                return await self.direct.download(
                    tb_file, filepath, progress_callback=progress_callback
                )
            session = await self.account._ensure_session()
            return await single_stream_download(
                tb_file.dlink,
                filepath,
                session=session,
                headers=headers,
                expected_size=size_bytes,
                stall_timeout=stall,
                progress_callback=progress_callback,
                logger=log,
            )
        except Exception:
            wipe_partial(filepath)
            raise

    async def list_files(self, url: str) -> TeraBoxResult:
        return await self.resolve(url, mode="list")

    async def get_download_links(self, url: str) -> TeraBoxResult:
        return await self.resolve(url, mode="download")

    async def get_stream_links(self, url: str, wait: bool = False) -> TeraBoxResult:
        return await self.resolve(url, mode="stream", wait_for_transcoding=wait)

    async def download(
        self,
        url: str,
        output_dir: str = ".",
        chunk_size: int = 8192,
        progress_callback: Optional[Callable] = None,
    ) -> list[str]:
        result = await self.resolve(url, mode="download")
        if not result.ok:
            raise TeraBoxError(f"Failed to resolve: {result.error}")

        os.makedirs(output_dir, exist_ok=True)
        downloaded: list[str] = []

        for tb_file in result.files:
            if not tb_file.dlink:
                continue
            filepath = os.path.join(output_dir, tb_file.filename)

            if tb_file.backend == "direct":
                await self.direct.download(tb_file, filepath)
                downloaded.append(filepath)
                continue

            try:
                response = await self.account._request_with_retry(
                    "GET", tb_file.dlink, headers={"User-Agent": self.account.USER_AGENT}
                )
                try:
                    total = int(response.headers.get("content-length", 0))
                    done = 0
                    with open(filepath, "wb") as handle:
                        async for chunk in response.content.iter_chunked(chunk_size):
                            handle.write(chunk)
                            done += len(chunk)
                            if progress_callback:
                                progress_callback(tb_file.filename, done, total)
                    downloaded.append(filepath)
                finally:
                    await response.release()
            except Exception as exc:
                raise TeraBoxError(f"Download failed for {tb_file.filename}: {exc}") from exc

        return downloaded

    def to_dict(self, result: TeraBoxResult) -> dict:
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

    async def validate_session(self) -> tuple[bool, str]:
        return await self.account.validate_session()

    def __repr__(self) -> str:
        ndus = self.account._cookies_dict.get("ndus", "")
        masked = f"{ndus[:8]}..." if len(ndus) > 8 else ndus
        tokens = "resolved" if self.account._tokens_resolved else "manual"
        return f"TeraBoxDownloader(ndus={masked!r}, tokens={tokens}, root={self.root_path!r})"

    async def __call__(self, url: str, mode: str = "download", **kwargs) -> TeraBoxResult:
        return await self.resolve(url, mode=mode, **kwargs)


# ── Multi-account pool (multi-ndus) ────────────────────────────────

class TeraBoxAccountPool:
    """Round-robin pool of :class:`TeraBoxDownloader` accounts (multi-ndus).

    Several TeraBox accounts share the load. A task walks accounts via
    :meth:`ordered_accounts` (starting from the next round-robin slot) and
    keeps the first account that resolves the share link successfully — that
    gives automatic fail-over when an account is blocked, expired or
    rate-limited (e.g. TeraBox ``need verify`` / quota errors).
    """

    def __init__(self, downloaders: list[TeraBoxDownloader]) -> None:
        self.logger = structlog.get_logger("TeraBoxAccountPool")
        self.accounts = [d for d in downloaders if d is not None]
        if not self.accounts:
            raise TeraBoxAuthError("TeraBoxAccountPool requires at least one account")
        self._cursor = 0

    def __len__(self) -> int:
        return len(self.accounts)

    @property
    def size(self) -> int:
        return len(self.accounts)

    def ordered_accounts(self) -> list[TeraBoxDownloader]:
        """Return every account, starting from the next round-robin slot."""
        n = len(self.accounts)
        if n == 0:
            return []
        start = self._cursor % n
        self._cursor = (start + 1) % n
        return [self.accounts[(start + i) % n] for i in range(n)]

    async def close(self) -> None:
        for account in self.accounts:
            try:
                await account.close()
            except Exception:
                pass

    def __repr__(self) -> str:
        heads = [
            account.account._cookies_dict.get("ndus", "")
            for account in self.accounts
        ]
        masked = ", ".join(f"{h[:8]}..." if len(h) > 8 else h for h in heads)
        return f"TeraBoxAccountPool({len(self.accounts)} accounts, ndus=[{masked}])"


# ── Module-level async download helper ────────────────────────────

async def download_file_async(
    downloader: TeraBoxDownloader,
    tb_file: TeraBoxFile,
    output_dir: str,
    progress_callback=None,
) -> str:
    """Backward-compatible wrapper for the pipeline."""
    return await downloader.download_file(
        tb_file, output_dir, progress_callback=progress_callback
    )
