"""Bysejikuar / Bikebyse downloader — AES-GCM decryption + HLS → yt-dlp."""

import base64
import json
import os
import re
import time
from urllib.parse import urlparse

from ..base import BaseDownloader

_DOMAIN_RE = re.compile(r"bysejikuar\.\w+|bikebyse\.\w+|byse\.com")


def _decrypt_playback(playback: dict) -> dict:
    """Decrypt AES-GCM encrypted playback payload."""
    from Cryptodome.Cipher import AES

    try:
        data_b64 = playback.get("data")
        iv_b64 = playback.get("iv")
        key_b64 = playback.get("key")
        tag_b64 = playback.get("tag")

        if not all([data_b64, iv_b64, key_b64]):
            return {}

        ciphertext = base64.b64decode(data_b64)
        iv = base64.b64decode(iv_b64)
        key = base64.b64decode(key_b64)
        tag = base64.b64decode(tag_b64) if tag_b64 else b""

        cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
        # GCM expects ciphertext + tag concatenated
        decrypted = cipher.decrypt_and_verify(ciphertext + tag)
        return json.loads(decrypted.decode("utf-8"))
    except Exception:
        return {}


class BysejikuarDownloader(BaseDownloader):
    """Download from Bysejikuar / Bikebyse and mirrors."""

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return bool(_DOMAIN_RE.search(host))

    def download(self, url: str, output_dir: str = "downloads") -> list:
        parsed = urlparse(url)
        vid = self._video_id(url)
        canonical = parsed.hostname or "bysejikuar.com"
        base = f"{parsed.scheme}://{canonical}"

        api_url = f"{base}/api/videos/{vid}"
        player_url = f"{base}/e/{vid}"

        resp = self._request(
            "GET",
            api_url,
            headers={"Referer": player_url, "Origin": base},
        )
        data = resp.json()

        playback = data.get("playback", {})
        sources_data = _decrypt_playback(playback)
        sources = sources_data.get("sources", [])

        if not sources:
            raise ValueError("No sources found in Bysejikuar playback data")

        m3u8_url = sources[0].get("url") if isinstance(sources[0], dict) else sources[0]
        if not m3u8_url:
            raise ValueError("Empty HLS URL in Bysejikuar sources")

        title = data.get("title") or vid
        return self._download_hls(m3u8_url, vid, output_dir, player_url, title)

    def _download_hls(
        self, m3u8_url: str, vid: str, output_dir: str, referer: str, title: str
    ) -> list:
        import yt_dlp

        fname = self._sanitize_filename(f"{title}.mp4")
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
        m = re.search(r"/(?:e|v|d|embed|videos|f|api/videos)/([A-Za-z0-9_-]+)", url)
        if m:
            return m.group(1)
        return urlparse(url).path.rstrip("/").split("/")[-1].split("?")[0]
