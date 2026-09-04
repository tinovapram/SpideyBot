"""Streamtape downloader."""

import os
import re
from urllib.parse import urlparse

from ..base import BaseDownloader


class StreamtapeDownloader(BaseDownloader):
    """Download videos from Streamtape (streamtape.cc, .com, .to, ...)."""

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return "streamtape" in host

    def download(self, url: str, output_dir: str = "downloads") -> list:
        if "/e/" in url:
            url = url.replace("/e/", "/v/")

        resp = self._request("GET", url)
        html = resp.text
        host = urlparse(url).hostname

        dl_url = self._extract_url(html, host)
        if not dl_url:
            raise ValueError("Could not extract Streamtape download URL")

        fname = self._title(html) or "streamtape_video.mp4"
        path = os.path.join(output_dir, self._sanitize_filename(fname))
        self._download_file(dl_url + "&dl=1", path, headers={"Referer": url})
        return [path]

    def _extract_url(self, html, host):
        match = re.search(
            r"getElementById\(['\"]norobotlink['\"]\)\.innerHTML\s*=\s*"
            r"['\"]([^'\"]+)['\"]\s*\+\s*\(['\"]([^'\"]+)['\"]\)"
            r"\.substring\((\d+)\)(?:\.substring\((\d+)\))?",
            html,
        )
        if match:
            prefix, rest, off = match.group(1), match.group(2), int(match.group(3))
            off2 = int(match.group(4)) if match.group(4) else 0
            return f"https:{prefix}{rest[off + off2:]}"

        match = re.search(
            r"getElementById\(['\"]captchalink['\"]\)\s*\.innerHTML\s*=\s*['\"]([^'\"]+)['\"]"
            r"\s*\+\s*\(['\"]([^'\"]+)['\"]\)\.substring\((\d+)\)",
            html,
        )
        if match:
            return f"https:{match.group(1)}{match.group(2)[int(match.group(3)):]}"

        match = re.search(
            r"getElementById\(['\"]ideoooolink['\"]\).*?=\s*['\"]([^'\"]+)['\"]\s*\+.*?"
            r"\(['\"]([^'\"]+)['\"]\)\.substring\((\d+)\)\.substring\((\d+)\)",
            html,
        )
        if match:
            prefix, raw, start, end = match.group(1), match.group(2), int(match.group(3)), int(match.group(4))
            return f"https://{(prefix + raw[start:][end:]).lstrip('/')}"

        for pattern in (r'href="(https?://[^"]*get_video\?[^"]+)"', r'href="(/get_video\?[^"]+)"'):
            match = re.search(pattern, html)
            if match:
                value = match.group(1)
                return value if value.startswith("http") else f"https://{host}{value}"

        return None

    @staticmethod
    def _title(html):
        match = re.search(r"<title>([^<]+)</title>", html)
        if not match:
            return None
        title = match.group(1).strip()
        for suffix in (" - Streamtape", " | Streamtape"):
            title = title.replace(suffix, "")
        return title if "." in title else f"{title}.mp4"
