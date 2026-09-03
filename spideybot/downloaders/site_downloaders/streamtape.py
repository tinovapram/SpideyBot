"""Streamtape downloader."""

import os
import re
from urllib.parse import urlparse

from .base import BaseDownloader


class StreamtapeDownloader(BaseDownloader):
    """Download videos from Streamtape (streamtape.cc, .com, .to, etc.)."""

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return "streamtape" in host

    def download(self, url: str, output_dir: str = "downloads") -> list:
        # /e/ pages are embed-only; redirect to /v/ for extraction
        if "/e/" in url:
            url = url.replace("/e/", "/v/")

        resp = self._request("GET", url)
        html = resp.text
        host = urlparse(url).hostname

        dl_url = self._extract_url(html, host)
        if not dl_url:
            raise ValueError("Could not extract Streamtape download URL")

        dl_url += "&dl=1"

        fname = self._title(html) or "streamtape_video.mp4"
        fp = os.path.join(output_dir, self._sanitize_filename(fname))
        self._download_file(dl_url, fp, headers={"Referer": url})
        return [fp]

    # -- URL extraction (multiple fallback patterns) -------------------------------

    def _extract_url(self, html, host):
        # 1. norobotlink JS expression: 'prefix' + ('rest').substring(a).substring(b)
        norobot_m = re.search(
            r"getElementById\(['\"]norobotlink['\"]\)\.innerHTML\s*=\s*"
            r"['\"]([^'\"]+)['\"]\s*\+\s*\(['\"]([^'\"]+)['\"]\)"
            r"\.substring\((\d+)\)(?:\.substring\((\d+)\))?",
            html,
        )
        if norobot_m:
            prefix = norobot_m.group(1)  # '//streamtape.cc/get_vid'
            rest = norobot_m.group(2)    # 'xcdeo?id=...'
            off = int(norobot_m.group(3))
            off2 = int(norobot_m.group(4)) if norobot_m.group(4) else 0
            return f"https:{prefix}{rest[off + off2:]}"

        # 2. JS-reconstructed URL (substring pattern)
        m = re.search(
            r"getElementById\(['\"]captchalink['\"]\)\s*\.innerHTML\s*=\s*['\"]([^'\"]+)['\"]\s*\+\s*\(['\"]([^'\"]+)['\"]\)\.substring\((\d+)\)",
            html,
        )
        if m:
            prefix = m.group(1)
            raw_str = m.group(2)
            offset = int(m.group(3))
            return f"https:{prefix}{raw_str[offset:]}"

        # 3. ideoooolink + double substring
        m = re.search(
            r"getElementById\(['\"]ideoooolink['\"]\).*?=\s*['\"]([^'\"]+)['\"]\s*\+.*?\(['\"]([^'\"]+)['\"]\)\.substring\((\d+)\)\.substring\((\d+)\)",
            html,
        )
        if m:
            prefix = m.group(1)
            s_str = m.group(2)
            start = int(m.group(3))
            end = int(m.group(4))
            return f"https://{(prefix + s_str[start:][end:]).lstrip('/')}"

        # 4. Direct href / get_video patterns
        for pat in [
            r'href="(https?://[^"]*get_video\?[^"]+)"',
            r'href="(/get_video\?[^"]+)"',
        ]:
            m = re.search(pat, html)
            if m:
                val = m.group(1)
                if val.startswith("http"):
                    return val
                return f"https://{host}{val}"

        return None

    # -- title extraction ----------------------------------------------------------

    @staticmethod
    def _title(html):
        m = re.search(r"<title>([^<]+)</title>", html)
        if m:
            title = m.group(1).strip()
            for suffix in (" - Streamtape", " | Streamtape"):
                title = title.replace(suffix, "")
            if "." not in title:
                title += ".mp4"
            return title
        return None
