"""Vidmonstr / Vidoy CDN downloader.

Extraction chain (pure HTTP, no browser needed):
  /d/{id} or /e/{id}
    → parse JS for ``iframeId`` + ``embedToken``
    → GET ``/ip129jk?id={iframeId}&t={embedToken}``
    → parse JS for ``playerPath`` (stream.php URL)
    → GET ``stream.php?bucket=...&id=...&t=...``
    → parse ``<source src="...">`` for direct .mp4 URL

Folder (/f/{id}) pages list ``/d/`` links which are processed individually.
"""

import os
import re
import time
from urllib.parse import urlparse

import structlog

from ..base import BaseDownloader

logger = structlog.get_logger(__name__)

_FOLDER_PAUSE_S = 1.0


class VidmonstrDownloader(BaseDownloader):
    """Download videos from vidmonstr.com / vidoy CDN."""

    @classmethod
    def matches(cls, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "vidmonstr.com" in host

    # ── Public API ────────────────────────────────────────────────

    def download(self, url: str, output_dir: str = "downloads") -> list:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        path = parsed.path.rstrip("/")

        if path.startswith("/f/"):
            return self._download_folder(url, base, output_dir)
        # /d/ and /e/ use the same extraction chain
        return self._download_single(url, output_dir)

    # ── Single video ──────────────────────────────────────────────

    def _download_single(self, url: str, output_dir: str) -> list:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        referer = f"{base}/"

        # Step 1: fetch /d/ or /e/ page → extract iframeId + embedToken
        resp = self._request("GET", url)
        html = resp.text

        iid = re.search(r"var iframeId\s*=\s*'([^']+)'", html)
        tok = re.search(r"var embedToken\s*=\s*'([^']+)'", html)
        if not iid or not tok:
            raise ValueError("Could not extract vidmonstr player tokens")

        # Step 2: fetch /ip129jk → extract playerPath (stream.php URL)
        embed_url = f"{base}/ip129jk?id={iid.group(1)}&t={tok.group(1)}"
        resp2 = self._request("GET", embed_url, headers={"Referer": referer})

        pp = re.search(r'playerPath\s*=\s*"([^"]+)"', resp2.text)
        if not pp:
            raise ValueError("Could not extract vidmonstr playerPath")
        player_url = pp.group(1).replace("\\u0026", "&")

        # Step 3: fetch stream.php → extract <source src="..."> video URL
        resp3 = self._request(
            "GET", player_url,
            headers={"Referer": referer},
        )

        video_url = self._extract_video(resp3.text)
        if not video_url:
            raise ValueError("Could not extract video URL from vidmonstr stream page")

        # Derive filename from page title or URL
        title = self._extract_title(html, resp3.text)
        ext = self._guess_ext(video_url, ".mp4")
        fname = f"{title}{ext}"
        path = os.path.join(output_dir, self._sanitize_filename(fname))
        self._download_file(
            video_url, path,
            headers={"Referer": player_url},
        )
        return [path]

    # ── Folder ────────────────────────────────────────────────────

    def _download_folder(self, url: str, base: str, output_dir: str) -> list:
        resp = self._request("GET", url)
        html = resp.text

        # Folder pages list full /d/ URLs
        links = re.findall(
            r'href=["\']((?:https?://[^"\']+)?/d/[a-z0-9]+)', html, re.I,
        )
        if not links:
            # Fallback: bare /d/ ids
            ids = re.findall(r'/d/([a-z0-9]+)', html)
            links = [f"{base}/d/{vid}" for vid in ids]
        if not links:
            raise ValueError("No files found in vidmonstr folder")

        files: list[str] = []
        seen: set[str] = set()
        for i, link in enumerate(links):
            full = link if link.startswith("http") else base + link
            if full in seen:
                continue
            seen.add(full)
            if i:
                time.sleep(_FOLDER_PAUSE_S)
            try:
                files.extend(self._download_single(full, output_dir))
            except Exception as exc:
                logger.warning(
                    "Vidmonstr folder item failed",
                    url=full, error=str(exc),
                )
        return files

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _extract_video(html: str) -> str | None:
        """Pull the direct video URL from stream.php HTML."""
        m = re.search(r'<source\s+src=["\']([^"\']+)', html, re.I)
        if m:
            return m.group(1)
        # Fallback: any .mp4/.m3u8 URL
        m = re.search(r'(https?://[^\s"<>]+\.(?:mp4|m3u8))', html)
        return m.group(1) if m else None

    @staticmethod
    def _extract_title(page_html: str, stream_html: str) -> str:
        """Best-effort title from VPLAYER JS or <title> tag."""
        m = re.search(r'title\s*:\s*"([^"]+)"', stream_html)
        if m:
            name = m.group(1).rsplit(".", 1)[0]  # strip .mp4 extension
            return name.strip()
        m = re.search(r"<title>([^<]+)</title>", page_html)
        if m:
            return m.group(1).strip()
        return "vidmonstr_video"

    @staticmethod
    def _guess_ext(url: str, default: str = ".mp4") -> str:
        path = urlparse(url).path.lower()
        for ext in (".mp4", ".m3u8", ".webm", ".mkv"):
            if path.endswith(ext):
                return ext
        return default
