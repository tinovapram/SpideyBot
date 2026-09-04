"""Doodstream / Playmogo downloader with dynamic domain support.

Doodstream and its mirrors (Playmogo, dooood, d000d, dood.video CDNs, ...)
enforce a fairly aggressive anti-bot:

* they drop or silently close reused CDN connections (``RemoteDisconnected``),
* the ``pass_md5`` endpoint answers ``RELOAD...`` when queried too fast, and
* frequently-hit IPs get served a Cloudflare Turnstile CAPTCHA page instead of
  the embed (no ``pass_md5`` path at all).

This module therefore treats those as *transient*, retries with backoff, never
builds a download URL from a ``RELOAD`` payload, and reports a clear message
when a CAPTCHA wall is hit. Folder downloads pace requests and keep going when
an individual file fails, instead of aborting the whole folder.
"""

import os
import random
import re
import time
from urllib.parse import urlparse

import requests

from ..base import BaseDownloader

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

# Transient failures Doodstream/CDN raise under load or on reused connections.
_TRANSIENT = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.Timeout,
)

# How many times to re-resolve + re-download a single file before giving up.
_MAX_ATTEMPTS = 3
# Backoff (seconds) between attempts: 1.5, 3, ...
_BACKOFF_STEP_S = 1.5
# Pause between files inside a folder to stay under the anti-bot radar.
_FOLDER_PAUSE_S = 1.0

_RELOAD_RE = re.compile(r"RELOAD", re.IGNORECASE)


class DoodstreamBlockedError(Exception):
    """Doodstream is showing an anti-bot CAPTCHA wall; retrying won't help."""


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

    # ── Folder ───────────────────────────────────────────────────

    def _download_folder(self, url, base, output_dir):
        resp = self._request("GET", url)
        links = re.findall(r'href="((?:https?://[^"/]+)?/d/[^"]+)"', resp.text)
        if not links:
            links = re.findall(r'data-href="((?:https?://[^"/]+)?/d/[^"]+)"', resp.text)
        if not links:
            raise ValueError("No files found in Doodstream folder")

        files, seen, failures = [], set(), []
        for i, link in enumerate(links):
            full = link if link.startswith("http") else base + link
            if full in seen:
                continue
            seen.add(full)

            # Pace requests: rapid bursts are what trigger doodstream's anti-bot.
            if i:
                time.sleep(_FOLDER_PAUSE_S)

            try:
                files.extend(self._download_from_page(full, base, output_dir))
            except DoodstreamBlockedError:
                # Hard CAPTCHA wall: don't keep hammering the remaining files.
                raise
            except Exception as exc:  # noqa: BLE001 - one bad file != dead folder
                failures.append(f"{full.rsplit('/', 1)[-1]}: {exc}")

        if not files and failures:
            detail = "; ".join(failures[:3])
            raise ValueError(f"Doodstream folder download failed. {detail}")
        return files

    # ── Single file page (/d/) ───────────────────────────────────

    def _download_from_page(self, url, base, output_dir):
        embed_url = url.replace("/d/", "/e/", 1)
        embed_base = base
        try:
            resp = self._request("GET", url)
            # Some mirrors put an absolute cross-host iframe in /d/; the common
            # doodstream layout uses a *relative* ``/e/...`` iframe, which the
            # ``url.replace`` fallback already covers.
            match = re.search(r'<iframe[^>]+src="(https?://[^"]+)"', resp.text)
            if match:
                parsed = urlparse(match.group(1))
                if parsed.hostname and parsed.hostname != urlparse(url).hostname:
                    embed_base = f"{parsed.scheme}://{parsed.hostname}"
                    embed_url = embed_base + match.group(1).split(parsed.hostname, 1)[1]
        except Exception:  # noqa: BLE001 - page fetch is best-effort
            pass
        return self._download_from_embed(embed_url, embed_base, output_dir)

    # ── Embed (/e/) → pass_md5 → direct CDN URL ──────────────────

    def _download_from_embed(self, embed_url, base, output_dir):
        last_error = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return self._attempt_file(embed_url, base, output_dir)
            except DoodstreamBlockedError:
                raise
            except _TRANSIENT as exc:
                last_error = exc
                time.sleep(_BACKOFF_STEP_S * attempt)
            except ValueError as exc:
                if _is_reload(str(exc)):
                    last_error = exc
                    time.sleep(_BACKOFF_STEP_S * attempt)
                else:
                    raise
        raise ValueError(
            f"Doodstream download failed after {_MAX_ATTEMPTS} attempts: {last_error}"
        )

    def _attempt_file(self, embed_url, base, output_dir):
        """One embed→pass_md5→direct-URL→file cycle (each token is one-shot)."""
        html = self._request("GET", embed_url).text

        match = re.search(r"/pass_md5/[^\s\"'<>]+", html)
        if not match:
            if _looks_like_captcha(html):
                raise DoodstreamBlockedError(
                    "Doodstream is showing an anti-bot CAPTCHA for this video. "
                    "Wait a few minutes and try again, or use a different network."
                )
            raise ValueError("Could not find pass_md5 path in Doodstream embed")

        pass_md5_path = match.group(0)
        token = pass_md5_path.rsplit("/", 1)[-1]

        resp = self._request("GET", base + pass_md5_path, headers={"Referer": embed_url})
        url_prefix = resp.text.strip()
        if not url_prefix:
            raise ValueError("Empty pass_md5 response from Doodstream")
        if not url_prefix.startswith("http"):
            # Anti-hotlink: pass_md5 answered "RELOAD..." because we asked too
            # fast. Never turn that into a download URL; signal a retry.
            raise ValueError(f"pass_md5 not ready (server replied {url_prefix[:24]!r})")

        hash_suffix = "".join(random.choices(_ALPHABET, k=10))
        dl_url = f"{url_prefix}{hash_suffix}?token={token}"

        fname = self._filename_from_cd(resp) or f"{token[:8]}.mp4"
        path = os.path.join(output_dir, self._sanitize_filename(fname))
        self._download_file(
            dl_url,
            path,
            # ``Connection: close`` stops requests from reusing a pooled CDN
            # connection that doodstream may have silently closed.
            headers={"Referer": embed_url, "Connection": "close"},
            progress_callback=self._progress_callback,
        )
        return [path]

    @staticmethod
    def _filename_from_cd(resp):
        cd = resp.headers.get("Content-Disposition", "")
        match = re.search(r'filename[^;=\n]*=["\']?([^"\';\n]+)', cd)
        return match.group(1).strip() if match else None


def _looks_like_captcha(html: str) -> bool:
    low = html.lower()
    return (
        "turnstile" in low
        or "captcha" in low
        or "cf-chl" in low
        or "challenge-platform" in low
    )


def _is_reload(message: str) -> bool:
    return bool(_RELOAD_RE.search(message))
