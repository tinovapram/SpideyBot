"""Vidara downloader — based on cloudstream's Vidara extractor."""

import os
import re
import subprocess
from urllib.parse import urlparse

from .base import BaseDownloader


class VidaraDownloader(BaseDownloader):
    """Download videos from Vidara and mirrors.

    Cloudstream pattern: extract filecode from URL path, POST to
    /api/stream on the ORIGINAL domain (not the embed domain).
    """

    _DOMAIN_RE = re.compile(r"vidara|vidar[ae]")

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return bool(cls._DOMAIN_RE.search(host))

    def download(self, url: str, output_dir: str = "downloads") -> list:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"

        # Cloudstream pattern: filecode = last path segment (strip query)
        filecode = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not filecode:
            raise ValueError(
                f"Could not extract filecode from Vidara URL: {url}"
            )

        dl_url = self._call_api(base, filecode, url)
        if not dl_url:
            raise ValueError("Could not extract Vidara download URL")

        fname = f"{filecode}.mp4"
        fp = os.path.join(output_dir, self._sanitize_filename(fname))
        os.makedirs(output_dir, exist_ok=True)

        if ".m3u8" in dl_url:
            # HLS stream — use yt-dlp (handles non-standard segment extensions)
            cmd = ["yt-dlp", "-o", fp, dl_url]
            subprocess.run(cmd, check=True, timeout=600)
        else:
            self._download_file(dl_url, fp, headers={"Referer": url})
        return [fp]

    def _call_api(self, api_base, filecode, referer):
        """POST to /api/stream — cloudstream sends filecode + device."""
        try:
            resp = self._request(
                "POST",
                f"{api_base}/api/stream",
                json_data={"filecode": filecode, "device": "web"},
                headers={"Referer": referer},
            )
            data = resp.json()
            src = data.get("streaming_url") or data.get("url")
            if src:
                return src
        except Exception:
            pass
        return None
