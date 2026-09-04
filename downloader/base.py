"""Base class for all site-specific downloaders."""

from __future__ import annotations

import json
import os
import time
from typing import Iterator

import requests
import structlog

from utils.files import sanitize_filename

logger = structlog.get_logger(__name__)


class BaseDownloader:
    """Common HTTP plumbing for site-specific downloaders.

    Subclasses implement ``fetch_media(url) -> dict`` and ``download(url,
    output_dir) -> list[str]``. The default ``download_streaming`` falls back
    to ``download``.
    """

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self) -> None:
        # Each instance owns a requests.Session (connection pooling). Safe for
        # single-threaded use inside ``run_in_executor``.
        self._session = requests.Session()
        self._session.headers.update(self.DEFAULT_HEADERS)
        self._progress_callback = None

    # ── Helpers for subclasses ─────────────────────────────────────

    def _sanitize_filename(self, filename: str) -> str:
        return sanitize_filename(filename)

    def _write_metadata(self, output_dir: str, meta: dict) -> str | None:
        """Write ``metadata.json`` into *output_dir* and return its path.

        The sidecar carries a native post caption/description that the upload
        pipeline reads to prepend to the first file's caption. Kept in the
        per-task staging directory so registry singletons never share state.
        Returns ``None`` when the write fails (caption silently skipped).
        """
        try:
            os.makedirs(output_dir, exist_ok=True)
            path = os.path.join(output_dir, "metadata.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, ensure_ascii=False, indent=4)
            return path
        except OSError as exc:
            logger.warning("Failed to write metadata.json", error=str(exc))
            return None

    def _request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        data: dict | None = None,
        json_data: dict | None = None,
        params: dict | None = None,
        timeout: int = 15,
        **kwargs,
    ) -> requests.Response:
        req_headers = dict(self.DEFAULT_HEADERS)
        if headers:
            req_headers.update(headers)

        response = self._session.request(
            method=method,
            url=url,
            headers=req_headers,
            data=data,
            json=json_data,
            params=params,
            timeout=timeout,
            **kwargs,
        )
        response.raise_for_status()
        return response

    def _download_file(
        self,
        url: str,
        file_path: str,
        headers: dict | None = None,
        progress_callback=None,
        proxies: dict | None = None,
    ) -> str:
        """Download *url* to *file_path* with optional progress callback."""
        req_headers = dict(self.DEFAULT_HEADERS)
        if headers:
            req_headers.update(headers)

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        response = self._session.get(url, headers=req_headers, stream=True, timeout=3600, proxies=proxies)
        response.raise_for_status()

        total = int(response.headers.get("Content-Length", 0))
        downloaded = 0
        last_cb = 0.0
        callback = progress_callback or self._progress_callback

        with open(file_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                if callback and time.time() - last_cb >= 5.0:
                    last_cb = time.time()
                    callback(downloaded, total)

        return file_path

    # ── Public API ─────────────────────────────────────────────────

    def download(self, url: str, output_dir: str = "downloads") -> list:
        raise NotImplementedError

    def download_streaming(self, url: str, output_dir: str = "downloads") -> Iterator[str]:
        """Yield file paths one by one. Defaults to ``download()``."""
        yield from self.download(url, output_dir=output_dir)


def unpack(p: str, a: int | None = None, c: int | None = None, k: list | None = None) -> str:
    """Deobfuscator for packed JavaScript (``eval(function(p,a,c,k,e,d){...}``).

    Used by MixDrop, StreamWish, Luluvdoo, and other sites that ship
    player code through Dean Edwards-style packers.
    """
    import ast as _ast
    import re as _re

    if a is None and c is None and k is None:
        pattern = r"\}\('(.*)',\s*(\d+),\s*(\d+),\s*'(.*)'\.split\('\|'\)"
        m = _re.search(pattern, p, _re.DOTALL)
        if not m:
            pattern2 = r'eval\(function\((.*?)\)\{.*?\}\((.*?)\)\)'
            m2 = _re.search(pattern2, p, _re.DOTALL)
            if not m2:
                return p
            raw = m2.group(2).replace(".split('|')", "")
            try:
                data = _ast.literal_eval(raw)
                p, a, c, k = data[0], int(data[1]), int(data[2]), data[3].split('|')
            except Exception:
                return p
        else:
            p, a, c, k = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")

    digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def base_encode(n):
        rem = n % a
        digit = digits[rem] if rem < len(digits) else str(rem)
        if n < a:
            return digit
        return base_encode(n // a) + digit

    d: dict[str, str] = {}
    for i in range(c - 1, -1, -1):
        key = base_encode(i)
        d[key] = k[i] if i < len(k) and k[i] else key

    def replace(match):
        w = match.group(0)
        return d.get(w, w)

    return _re.sub(r"\b\w+\b", replace, p)


def find_url(obj, *, patterns=(".mp4",)) -> str | None:
    """Recursively search a JSON-like object for the first matching URL."""
    if isinstance(obj, dict):
        for value in obj.values():
            found = find_url(value, patterns=patterns)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_url(value, patterns=patterns)
            if found:
                return found
    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
        if any(pattern in obj for pattern in patterns):
            return obj
    return None
