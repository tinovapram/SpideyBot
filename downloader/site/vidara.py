"""Vidara downloader — based on cloudstream's Vidara extractor.

Vidara frequently changes domains, so ``matches()`` uses two tiers:
1. Fast regex for known domain patterns (zero network cost).
2. Fingerprint probe — fetches the page and checks for Vidara-specific
   markers (``ENCRYPTION_KEY``, ``/api/stream``).  Matches are cached
   in ``_FINGERPRINT_CACHE`` so each new domain is only probed once.
"""

import os
import re
import time
from urllib.parse import urlparse

import requests as _req

from ..base import BaseDownloader

# Known Vidara domain patterns (fast, no network).
_DOMAIN_RE = re.compile(r"vidara|vidar[ae]")

# HTML markers unique to Vidara pages.
_FINGERPRINTS = ("ENCRYPTION_KEY", "/api/stream")
_FINGERPRINT_TIMEOUT = 8  # seconds — short so we don't stall detection


class VidaraDownloader(BaseDownloader):
    """Download videos from Vidara and mirrors."""

    # Domains confirmed via fingerprint probe (persists for the process lifetime).
    _FINGERPRINT_CACHE: set[str] = set()

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        # Tier 1: known pattern — instant.
        if _DOMAIN_RE.search(host):
            return True
        # Tier 2: already-probed this process — instant.
        if host in cls._FINGERPRINT_CACHE:
            return True
        # Tier 3: fingerprint probe — one HTTP GET, then cache result.
        return cls._fingerprint_probe(url, host)

    @classmethod
    def _fingerprint_probe(cls, url: str, host: str) -> bool:
        try:
            resp = _req.get(url, timeout=_FINGERPRINT_TIMEOUT, headers={
                "User-Agent": "Mozilla/5.0"
            })
            body = resp.text[:64_000]  # first 64 KB is plenty
            if all(fp in body for fp in _FINGERPRINTS):
                cls._FINGERPRINT_CACHE.add(host)
                return True
        except Exception:
            pass
        return False

    def download(self, url: str, output_dir: str = "downloads") -> list:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        filecode = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not filecode:
            raise ValueError(f"Could not extract filecode from Vidara URL: {url}")

        dl_url = self._call_api(base, filecode, url)
        if not dl_url:
            raise ValueError("Could not extract Vidara download URL")

        path = os.path.join(output_dir, self._sanitize_filename(f"{filecode}.mp4"))
        os.makedirs(output_dir, exist_ok=True)

        if ".m3u8" in dl_url:
            self._download_m3u8(dl_url, path)
        else:
            self._download_file(dl_url, path, headers={"Referer": url})
        return [path]

    def _download_m3u8(self, dl_url: str, path: str) -> None:
        import yt_dlp

        last_call = [0.0]

        def progress_hook(data):
            if data.get("status") == "downloading" and self._progress_callback:
                downloaded = data.get("downloaded_bytes") or 0
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                now = time.monotonic()
                if downloaded and total and now - last_call[0] >= 5.0:
                    last_call[0] = now
                    self._progress_callback(downloaded, total)

        with yt_dlp.YoutubeDL({"outtmpl": path, "progress_hooks": [progress_hook], "quiet": True}) as ydl:
            ydl.download([dl_url])

    def _call_api(self, api_base, filecode, referer):
        try:
            resp = self._request(
                "POST",
                f"{api_base}/api/stream",
                json_data={"filecode": filecode, "device": "web"},
                headers={"Referer": referer},
            )
            data = resp.json()
            return data.get("streaming_url") or data.get("url")
        except Exception:
            return None
