"""MixDrop downloader — JS-packed MDCore.wurl extraction + MP4 download."""

import ast
import os
import re
from urllib.parse import urlparse

from ..base import BaseDownloader, unpack

_DOMAIN_RE = re.compile(
    r"mixdrop\.\w+|miiixdrop\.\w+|mixdrop\.(ag|co|to|sx|bz|ch|is|vc|club|si|nu|vip|ps|cat|click|space)"
)

# Mirrors to try when the primary domain is down.
_MIRRORS = [
    "mixdrop.ag",
    "mixdrop.co",
    "mixdrop.to",
    "mixdrop.sx",
    "mixdrop.bz",
]


class MixDropDownloader(BaseDownloader):
    """Download from MixDrop and mirror domains."""

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return bool(_DOMAIN_RE.search(host))

    def download(self, url: str, output_dir: str = "downloads") -> list:
        vid = self._video_id(url)
        parsed = urlparse(url)
        original_mirror = parsed.hostname or _MIRRORS[0]

        # Try the original mirror first, then fallbacks.
        mirrors = [original_mirror] + [m for m in _MIRRORS if m != original_mirror]

        last_error = None
        for mirror in mirrors:
            try:
                return self._extract_from_mirror(mirror, vid, output_dir)
            except Exception as exc:
                last_error = exc
                continue
        raise ValueError(f"MixDrop download failed on all mirrors: {last_error}")

    def _extract_from_mirror(self, mirror: str, vid: str, output_dir: str) -> list:
        player_url = f"https://{mirror}/e/{vid}"
        resp = self._request("GET", player_url, headers={"Referer": f"https://{mirror}/"})
        html = resp.text

        if "File was deleted" in html or "file not found" in html.lower():
            raise ValueError("File was deleted or not found on MixDrop")

        # Extract packed JS and unpack
        data_match = re.search(r'eval\(function\((.*?)\)\{.*?\}\((.*?)\)\)', html, re.DOTALL)
        if not data_match:
            raise ValueError("Could not find packed JS in MixDrop embed")

        raw = data_match.group(2).replace(".split('|')", "")
        try:
            data = ast.literal_eval(raw)
        except (ValueError, SyntaxError) as exc:
            raise ValueError(f"Failed to parse MixDrop packed data: {exc}") from exc

        p, a, c, k = data[0], int(data[1]), int(data[2]), data[3].split('|')
        decoded = unpack(p, a, c, k)

        # Extract MDCore.wurl
        url_match = re.search(r'MDCore\.wurl="([^"]+)"', decoded)
        if not url_match:
            raise ValueError("Could not find MDCore.wurl in MixDrop player")

        raw_url = url_match.group(1)
        stream_url = "https:" + raw_url if raw_url.startswith("//") else raw_url

        # Extract title
        title_match = re.search(r'MDCore\.title="([^"]+)"', decoded)
        title = title_match.group(1) if title_match else "MixDrop Video"

        fname = self._sanitize_filename(f"{vid}.mp4")
        path = os.path.join(output_dir, fname)
        self._download_file(
            stream_url,
            path,
            headers={"Referer": player_url},
            progress_callback=self._progress_callback,
        )
        return [path]

    @staticmethod
    def _video_id(url: str) -> str:
        m = re.search(r"/(?:e|v|d|embed|videos|f)/([A-Za-z0-9_-]+)", url)
        if m:
            return m.group(1)
        return urlparse(url).path.rstrip("/").split("/")[-1].split("?")[0]
