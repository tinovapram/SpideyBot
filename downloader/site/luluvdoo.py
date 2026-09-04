"""Luluvdoo / Lulustream downloader — packed JS → HLS playlist → yt-dlp."""

import ast
import os
import re
import time
from urllib.parse import urlparse

from ..base import BaseDownloader, unpack

_DOMAIN_RE = re.compile(
    r"luluvdoo\.\w+|luluvdo\.\w+|luluvid\.\w+|lulustream\.\w+|"
    r"luluvstream\.\w+|luluvdo\.net|lulustream\.to|luluvdoo\.to"
)


class LuluvdooDownloader(BaseDownloader):
    """Download from Luluvdoo / Lulustream and mirrors."""

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return bool(_DOMAIN_RE.search(host))

    def download(self, url: str, output_dir: str = "downloads") -> list:
        parsed = urlparse(url)
        vid = self._video_id(url)

        if "/e/" not in parsed.path:
            base = f"{parsed.scheme}://{parsed.hostname}"
            player_url = f"{base}/e/{vid}"
        else:
            player_url = url
            base = f"{parsed.scheme}://{parsed.hostname}"

        resp = self._request("GET", player_url, headers={"Referer": f"{base}/"})
        html = resp.text

        if "File is no longer available" in html or "file was deleted" in html.lower():
            raise ValueError("File was deleted on Luluvdoo")

        # Unpack the JS blob
        packed_match = re.search(
            r"(eval\(function\(p,a,c,k,e,d\)\{.*?\.split\('\|'\)[^)]*\)\))",
            html,
            re.DOTALL,
        )
        if not packed_match:
            raise ValueError("Could not find packed JS in Luluvdoo embed")

        decoded = unpack(packed_match.group(1))

        # Extract HLS streams
        streams = dict(re.findall(r'"(hls[234])"\s*:\s*"([^"]+)"', decoded))
        extra = re.findall(r'https?://[^\s"\']+\.(?:m3u8|txt|mp4)\??[^\s"\']*', decoded)
        for u in extra:
            if u not in streams.values():
                key = "hls2" if ".m3u8" in u else ("hls3" if ".txt" in u else "hls4")
                streams.setdefault(key, u)

        selected = streams.get("hls2") or streams.get("hls3") or streams.get("hls4")
        if not selected and streams:
            selected = list(streams.values())[0]
        if not selected:
            raise ValueError("No HLS stream found in Luluvdoo embed")

        return self._download_hls(selected, vid, output_dir, player_url)

    def _download_hls(self, m3u8_url: str, vid: str, output_dir: str, referer: str) -> list:
        import yt_dlp

        fname = self._sanitize_filename(f"{vid}.mp4")
        out_path = os.path.join(output_dir, fname)
        os.makedirs(output_dir, exist_ok=True)

        last_call = [0.0]

        def progress_hook(data):
            if data.get("status") == "downloading" and self._progress_callback:
                downloaded = data.get("downloaded_bytes") or 0
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                now = time.monotonic()
                if downloaded and total and now - last_call[0] >= 5.0:
                    last_call[0] = now
                    self._progress_callback(downloaded, total)

        opts = {
            "outtmpl": out_path,
            "progress_hooks": [progress_hook],
            "quiet": True,
            "http_headers": {"Referer": referer},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([m3u8_url])
        return [out_path]

    @staticmethod
    def _video_id(url: str) -> str:
        m = re.search(r"/(?:e|v|d|embed|videos|f)/([A-Za-z0-9_-]+)", url)
        if m:
            return m.group(1)
        return urlparse(url).path.rstrip("/").split("/")[-1].split("?")[0]
