"""One-shot script to rewrite streamtape.py _extract_url with norobotlink pattern."""
import pathlib

CONTENT = r'''"""Streamtape downloader."""

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

    # ── URL extraction (multiple fallback patterns) ─────────────────

    def _extract_url(self, html, host):
        # 1. norobotlink + ideoooolink + token (standard Streamtape pattern)
        token_m = re.search(
            r"getElementById\(['\"]norobotlink['\"]\)\.innerHTML\s*=\s*(.+?);",
            html,
        )
        if token_m:
            token_val = re.search(r"token=([^&'\"]+)", token_m.group(1))
            if token_val:
                token = token_val.group(1)
                io_m = re.search(
                    r"getElementById\(['\"]ideoooolink['\"]\)\.innerHTML\s*=\s*['\"]([^'\"]+)['\"]",
                    html,
                )
                if io_m:
                    return f"https:{io_m.group(1)}&token={token}"
                div_text_m = re.search(
                    r"<div[^>]*id=['\"]ideoooolink['\"][^>]*>([^<]+)</div>",
                    html,
                )
                if div_text_m:
                    return f"https:{div_text_m.group(1)}&token={token}"

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
'''

p = pathlib.Path("spideybot/downloaders/site_downloaders/streamtape.py")
p.write_text(CONTENT, encoding="utf-8")
print(f"Written {p} ({p.stat().st_size} bytes)")
