"""Doodstream / Playmogo downloader with dynamic domain support."""

import os
import random
import re
from urllib.parse import urlparse

from ..base import BaseDownloader

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


class DoodstreamDownloader(BaseDownloader):
    """Download from Doodstream/Playmogo and mirror domains.

    Supports ``/d/`` (single video), ``/f/`` (folder) and ``/e/`` (embed)
    paths, matching frequently-changing domains via regex.
    """

    _DOMAIN_RE = re.compile(
        r"dood(?:stream)?\.\w+|dooood\.\w+|doods\.pro|d00+d\.\w+|"
        r"ds(?:2play|2video|vplay)\.\w+|playmogo\.\w+|vide0\.net|myvidplay\.\w+"
    )

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return bool(cls._DOMAIN_RE.search(host))

    def download(self, url: str, output_dir: str = "downloads") -> list:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        path = parsed.path.rstrip("/")

        if path.startswith("/f/"):
            return self._download_folder(url, base, output_dir)
        if path.startswith("/e/"):
            return self._download_from_embed(url, base, output_dir)
        if path.startswith("/d/"):
            return self._download_from_page(url, base, output_dir)
        raise ValueError(f"Unsupported Doodstream URL format: {url}")

    def _download_folder(self, url, base, output_dir):
        resp = self._request("GET", url)
        links = re.findall(r'href="((?:https?://[^"/]+)?/d/[^"]+)"', resp.text)
        if not links:
            links = re.findall(r'data-href="((?:https?://[^"/]+)?/d/[^"]+)"', resp.text)
        if not links:
            raise ValueError("No files found in Doodstream folder")

        files, seen = [], set()
        for link in links:
            full = link if link.startswith("http") else base + link
            if full in seen:
                continue
            seen.add(full)
            files.extend(self._download_from_page(full, base, output_dir))
        return files

    def _download_from_page(self, url, base, output_dir):
        embed_url = url.replace("/d/", "/e/", 1)
        embed_base = base
        try:
            resp = self._request("GET", url)
            match = re.search(r'<iframe[^>]+src="(https?://[^"]+)"', resp.text)
            if match:
                parsed = urlparse(match.group(1))
                if parsed.hostname and parsed.hostname != urlparse(url).hostname:
                    embed_base = f"{parsed.scheme}://{parsed.hostname}"
                    embed_url = embed_base + match.group(1).split(parsed.hostname, 1)[1]
        except Exception:
            pass
        return self._download_from_embed(embed_url, embed_base, output_dir)

    def _download_from_embed(self, embed_url, base, output_dir):
        resp = self._request("GET", embed_url)
        html = resp.text

        match = re.search(r"/pass_md5/[^\s\"'<>]+", html)
        if not match:
            raise ValueError("Could not find pass_md5 path in Doodstream embed")

        pass_md5_path = match.group(0)
        token = pass_md5_path.rsplit("/", 1)[-1]

        resp = self._request("GET", base + pass_md5_path, headers={"Referer": embed_url})
        url_prefix = resp.text.strip()
        if not url_prefix:
            raise ValueError("Empty pass_md5 response from Doodstream")

        hash_suffix = "".join(random.choices(_ALPHABET, k=10))
        dl_url = f"{url_prefix}{hash_suffix}?token={token}"

        fname = self._filename_from_cd(resp) or f"{token[:8]}.mp4"
        path = os.path.join(output_dir, self._sanitize_filename(fname))
        self._download_file(
            dl_url, path, headers={"Referer": embed_url}, progress_callback=self._progress_callback
        )
        return [path]

    @staticmethod
    def _filename_from_cd(resp):
        cd = resp.headers.get("Content-Disposition", "")
        match = re.search(r'filename[^;=\n]*=["\']?([^"\';\n]+)', cd)
        return match.group(1).strip() if match else None
