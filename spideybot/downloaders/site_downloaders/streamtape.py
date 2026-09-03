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
        resp = self._request("GET", url)
        html = resp.text
        host = urlparse(url).hostname

        dl_url = self._extract_url(html, host)
        if not dl_url:
            raise ValueError("Could not extract Streamtape download URL")

        # Player appends &stream=1 for direct streaming
        if "&stream=" not in dl_url and "?stream=" not in dl_url:
            dl_url += "&stream=1"

        fname = self._title(html) or "streamtape_video.mp4"
        fp = os.path.join(output_dir, self._sanitize_filename(fname))
        self._download_file(dl_url, fp, headers={"Referer": url})
        return [fp]

    # ── URL extraction (multiple fallback patterns) ─────────────────

    def _extract_url(self, html, host):
        # 1. JS-reconstructed URL (cloudstream-style: parse JS substring rebuild)
        #    The HTML div tokens are decoys — JS replaces them at runtime.
        m = re.search(
            r"getElementById\(['\"]captchalink['\"]\)\s*\.innerHTML\s*=\s*['\"]([^'\"]+)['\"]\s*\+\s*\(['\"]([^'\"]+)['\"]\)\.substring\((\d+)\)",
            html,
        )
        if m:
            prefix = m.group(1)  # e.g. '//streamtape'
            raw_str = m.group(2)  # e.g. 'defge.cc/get_video?...'
            offset = int(m.group(3))
            path = raw_str[offset:]  # 'e.cc/get_video?...'
            return f"https:{prefix}{path}"

        # 2. JS substring reconstruction of #ideoooolink content
        # Pattern: document.getElementById('ideoooolink').innerHTML = ... + ('...substring chain...');
        m = re.search(
            r"getElementById\(['\"]ideoooolink['\"]\)\s*\.innerHTML\s*=\s*['\"]([^'\"]+)['\"]\s*\+",
            html,
        )
        if m:
            prefix = m.group(1)  # e.g. "/streamtape"
            # Find the string that gets substring'd: ('...')
            m2 = re.search(
                r"getElementById\(['\"]ideoooolink['\"]\).*?=\s*['\"]([^'\"]+)['\"]\s*\+.*?\(['\"]([^'\"]+)['\"]\)\.substring\((\d+)\)\.substring\((\d+)\)",
                html,
            )
            if m2:
                s_str = m2.group(2)
                start = int(m2.group(3))
                end = int(m2.group(4))
                rebuilt = prefix + "" + s_str[start:][end:]
                return f"https://{rebuilt.lstrip('/')}"



        # 4. Direct href patterns
        for pat in [
            r'href="(https?://[^"]*get_video\?[^"]+)"',
            r'href="(/get_video\?[^"]+)"',
            r"['\"](/get_video\?[^'\"]+)['\"]",
        ]:
            m = re.search(pat, html)
            if m:
                val = m.group(1)
                if val.startswith("http"):
                    return val
                return f"https://{host}{val}"

        # 5. video_url variable in JS
        m = re.search(r"video_url\s*=\s*['\"]([^'\"]+/get_video[^'\"]*)", html)
        if m:
            val = m.group(1)
            if val.startswith("/"):
                return f"https://{host}{val}"
            return val

        return None

    # ── title extraction ────────────────────────────────────────────

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
