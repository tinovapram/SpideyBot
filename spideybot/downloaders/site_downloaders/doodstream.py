"""Doodstream / Playmogo downloader with dynamic domain support."""

import os
import random
import re
from urllib.parse import urlparse

from .base import BaseDownloader

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


class DoodstreamDownloader(BaseDownloader):
    """Download from Doodstream/Playmogo and mirror domains.

    Supports ``/d/`` (single video), ``/f/`` (folder/gallery), and
    ``/e/`` (embed) URL paths.  Handles frequently-changing domains
    (dood.me, playmogo.com, ds2play.com, etc.) via regex matching.
    """

    _DOMAIN_RE = re.compile(
        r"dood(?:stream)?\.\w+|"    # dood.*, doodstream.*
        r"dooood\.\w+|"             # dooood.*
        r"doods\.pro|"              # doods.pro
        r"d00+d\.\w+|"              # d0000d.*, d000d.*
        r"ds(?:2play|2video|vplay)\.\w+|"  # ds2play.*, ds2video.*, dsvplay.*
        r"playmogo\.\w+|"           # playmogo.*
        r"vide0\.net|"              # vide0.net
        r"myvidplay\.\w+"           # myvidplay.*
    )

    @classmethod
    def matches(cls, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return bool(cls._DOMAIN_RE.search(host))

    # ── public entry point ──────────────────────────────────────────

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

    # ── folder (/f/) ────────────────────────────────────────────────

    def _download_folder(self, url, base, output_dir):
        resp = self._request("GET", url)
        # Extract unique /d/ links (relative or full URLs)
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

    # ── single video (/d/) ──────────────────────────────────────────

    def _download_from_page(self, url, base, output_dir):
        # Cloudstream pattern: /d/ → /e/ is the same base, just swap path.
        # No need to parse iframe — the embed lives at /e/{id} on the same host.
        embed_url = url.replace("/d/", "/e/", 1)
        embed_base = base
        # But if the page has a cross-domain iframe, honour it.
        try:
            resp = self._request("GET", url)
            m = re.search(r'<iframe[^>]+src="(https?://[^"]+)"', resp.text)
            if m:
                ep = urlparse(m.group(1))
                if ep.hostname and ep.hostname != urlparse(url).hostname:
                    embed_base = f"{ep.scheme}://{ep.hostname}"
                    embed_url = embed_base + m.group(1).split(ep.hostname, 1)[1]
        except Exception:
            pass  # fallback to same-domain /e/ URL
        return self._download_from_embed(embed_url, embed_base, output_dir)

    # ── embed (/e/) → pass_md5 → download (cloudstream pattern) ─────

    def _download_from_embed(self, embed_url, base, output_dir):
        resp = self._request("GET", embed_url)
        html = resp.text

        # 1. Find /pass_md5/{token_path} in embed HTML (cloudstream pattern)
        m = re.search(r"/pass_md5/[^\s\"'<>]+", html)
        if not m:
            raise ValueError("Could not find pass_md5 path in Doodstream embed")
        pass_md5_path = m.group(0)
        pass_md5_url = base + pass_md5_path
        token = pass_md5_path.rsplit("/", 1)[-1]

        # 2. Fetch pass_md5 endpoint — server returns the raw download URL prefix
        resp = self._request("GET", pass_md5_url, headers={"Referer": embed_url})
        url_prefix = resp.text.strip()
        if not url_prefix:
            raise ValueError("Empty pass_md5 response from Doodstream")

        # 3. Append random 10-char hash + ?token=<last segment>
        hash_suffix = "".join(random.choices(_ALPHABET, k=10))
        dl_url = f"{url_prefix}{hash_suffix}?token={token}"

        fname = self._filename_from_cd(resp) or f"{token[:8]}.mp4"
        fp = os.path.join(output_dir, self._sanitize_filename(fname))
        self._download_file(dl_url, fp, headers={"Referer": embed_url})
        return [fp]

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _filename_from_cd(resp):
        cd = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename[^;=\n]*=["\']?([^"\';\n]+)', cd)
        return m.group(1).strip() if m else None
