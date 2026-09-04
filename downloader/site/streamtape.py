"""Streamtape downloader — with /e/ embed and /get_video redirect support."""

import os
import re
from urllib.parse import urljoin, urlparse

from ..base import BaseDownloader


_DOMAIN_RE = re.compile(
    r"streamtape\.\w+|streamta\.\w+|stape\.fun"
)


class StreamtapeDownloader(BaseDownloader):
    """Download videos from Streamtape (streamtape.cc, .com, .to, streamta.site ...)."""

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return bool(_DOMAIN_RE.search(host))

    def download(self, url: str, output_dir: str = "downloads") -> list:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        host = parsed.hostname

        # /e/ embed → resolve to /v/ view page
        if "/e/" in parsed.path:
            video_id = parsed.path.rstrip("/").split("/")[-1]
            view_url = f"{base}/v/{video_id}"
        else:
            view_url = url

        # Try to resolve signed /get_video redirect first
        dl_url = self._try_get_video_redirect(view_url, base)
        if dl_url:
            fname = "streamtape_video.mp4"
            path = os.path.join(output_dir, self._sanitize_filename(fname))
            self._download_file(dl_url, path, headers={"Referer": view_url})
            return [path]

        # Fallback to classic HTML extraction
        resp = self._request("GET", view_url)
        html = resp.text

        dl_url = self._extract_url(html, host)
        if not dl_url:
            raise ValueError("Could not extract Streamtape download URL")

        fname = self._title(html) or "streamtape_video.mp4"
        path = os.path.join(output_dir, self._sanitize_filename(fname))
        self._download_file(dl_url + "&dl=1", path, headers={"Referer": view_url})
        return [path]

    def _try_get_video_redirect(self, view_url: str, base: str) -> str | None:
        """Try extracting a signed /get_video URL from the page."""
        try:
            resp = self._request("GET", view_url)
            html = resp.text
        except Exception:
            return None

        for pattern in (
            r'href="(/get_video\?[^"]+)"',
            r"href='(/get_video\?[^']+)'",
            r'(?:"|/)(get_video\?[^"\'&\s]+(?:&[^"\'&\s]+)+)',
        ):
            m = re.search(pattern, html)
            if m:
                path = m.group(1)
                full_url = urljoin(base + "/", path)
                try:
                    check = self._request("GET", full_url, headers={"Referer": view_url})
                    if check.status_code == 200:
                        return full_url
                except Exception:
                    continue
        return None

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
