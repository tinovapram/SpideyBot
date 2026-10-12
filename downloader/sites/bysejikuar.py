"""Bysejikuar native resolver — AES-GCM decryption → HLS URL.

Behaviour grounded in V5 `downloader/site/bysejikuar.py`. Extraction chain:

  /v/{id} or /e/{id}
    → fetch page
    → extract encrypted data and key/iv, AES-GCM decrypt it
    → hand the resulting playlist to the fetcher as an ``hls`` transfer, consumed with ffmpeg

The playlist is not a file: an ``.m3u8`` is a manifest, so it is resolved as an ``hls`` transfer
rather than fetched as bytes that would land on disk as a text file named ``.mp4``.
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

_HOST_RE = re.compile(r"bysejikuar\.(com|to|co|cc|site|net|xyz)")

# Page patterns
_ENCRYPTED_RE = re.compile(r'encrypted["\']?\s*[:=]\s*["\']([^"\']+)')
_KEY_RE = re.compile(r'key["\']?\s*[:=]\s*["\']([^"\']+)')
_IV_RE = re.compile(r'iv["\']?\s*[:=]\s*["\']([^"\']+)')

DEFAULT_TIMEOUT = bridge.DEFAULT_TIMEOUT


def _host_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


def matches(url: str) -> bool:
    return bool(_HOST_RE.search(_host_of(url)))


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> Resolution:
    session = client or bridge.session_for(timeout, referer="https://bysejikuar.com/")
    try:
        await bridge.load_cookies(session, cookies_file, default_domain=".bysejikuar.com")

        # Step 1: fetch page
        html = await bridge.get_text(session, url)

        if "File not found" in html or "deleted" in html.lower():
            raise ResolveError("File not found or deleted on Bysejikuar")

        # Step 2: extract encrypted data, key, and iv
        encrypted_b64 = _extract_encrypted(html)
        key_b64 = _extract_key(html)
        iv_b64 = _extract_iv(html)

        if not encrypted_b64 or not key_b64 or not iv_b64:
            raise ResolveError("Could not extract Bysejikuar encrypted data/key/iv")

        # Step 3: AES-GCM decrypt
        hls_url = _decrypt_hls(encrypted_b64, key_b64, iv_b64)
        if not hls_url:
            raise ResolveError("Failed to decrypt Bysejikuar HLS URL")

        # Step 4: name the file from the page title
        filename = bridge.filename_for(
            _extract_title(html) or "bysejikuar_video", extension=".mp4"
        )

        return bridge.resolution(
            "bysejikuar", url, [bridge.media(hls_url, filename, role="hls", source_url=url)]
        )
    finally:
        if client is None:
            await bridge.close(session)


def _extract_encrypted(html: str) -> str | None:
    m = _ENCRYPTED_RE.search(html)
    if m:
        return m.group(1)
    return None


def _extract_key(html: str) -> str | None:
    m = _KEY_RE.search(html)
    if m:
        return m.group(1)
    return None


def _extract_iv(html: str) -> str | None:
    m = _IV_RE.search(html)
    if m:
        return m.group(1)
    return None


def _decrypt_hls(encrypted_b64: str, key_b64: str, iv_b64: str) -> str | None:
    """Decrypt the AES-GCM payload the page carries.

    The nonce comes from the payload's own first 12 bytes rather than from the page's ``iv`` field,
    which is why ``iv_b64`` is only checked for presence by the caller: on every sample the site
    served, the field is a constant decoration and the payload is self-contained.
    """
    try:
        encrypted = base64.b64decode(encrypted_b64)
        key = base64.b64decode(key_b64)

        # AES-GCM: first 12 bytes are nonce, last 16 bytes are tag
        if len(encrypted) < 28:
            return None

        nonce = encrypted[:12]
        ciphertext = encrypted[12:-16]
        tag = encrypted[-16:]

        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext + tag, None)
        return plaintext.decode("utf-8")
    except Exception as e:
        log.debug("Bysejikuar decryption failed: %s", e)
        return None


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        title = m.group(1).strip()
        for suffix in (" - Bysejikuar", " | Bysejikuar"):
            title = title.replace(suffix, "")
        return title
    return None


def resolver(
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="bysejikuar", attempt=_attempt)