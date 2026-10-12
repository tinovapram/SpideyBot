"""MEGA native resolver — custom crypto protocol.

Behaviour grounded in V5 `downloader/site/mega.py`. Extraction chain:

  /file/{id} or /folder/{id}
    → fetch page
    → extract file key and attributes
    → decrypt file key with master key
    → request download URL from API
    → return as single-file transfer

Note: This is a simplified implementation. Full MEGA support requires
the complete crypto protocol (RSA, AES, key derivation).
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from curl_cffi import requests

from downloader.contracts import Resolution, ResolvedFile, ResolveError, Transfer
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

_HOST_RE = re.compile(r"mega\.(nz|co\.nz|io)")

# Page patterns
_FILE_KEY_RE = re.compile(r'fileKey["\']?\s*[:=]\s*["\']([^"\']+)')
_FILE_ATTR_RE = re.compile(r'fileAttr["\']?\s*[:=]\s*["\']([^"\']+)')
_FILE_ID_RE = re.compile(r'/file/([^/?#]+)')

DEFAULT_TIMEOUT = 60.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _host_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


def matches(url: str) -> bool:
    return bool(_HOST_RE.search(_host_of(url)))


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolution:
    client = bridge.session_for(timeout, referer="https://mega.nz/")
    try:
        await bridge.load_cookies(client, cookies_file, default_domain=".mega.nz")

        # Step 1: fetch page
        html = await bridge.get_text(client, url)

        if "File not found" in html or "deleted" in html.lower():
            raise ResolveError("File not found or deleted on MEGA")

        # Step 2: extract file ID from URL
        file_id = _extract_file_id(url)
        if not file_id:
            raise ResolveError("Could not extract MEGA file ID")

        # Step 3: extract file key and attributes from page
        file_key_b64 = _extract_file_key(html)
        file_attr_b64 = _extract_file_attr(html)

        if not file_key_b64:
            raise ResolveError("Could not extract MEGA file key")

        # Step 4: decrypt file attributes to get filename
        filename = "mega_file"
        if file_attr_b64:
            filename = _decrypt_attr(file_attr_b64, file_key_b64) or filename

        # Step 5: request download URL from MEGA API
        download_url = await _get_download_url(client, file_id, file_key_b64)
        if not download_url:
            raise ResolveError("Failed to get MEGA download URL")

        return Resolution(
            engine="mega",
            source_url=url,
            files=(
                ResolvedFile(
                    transfers=(Transfer(url=download_url, role="video", filesize=None),),
                    filename=filename,
                    source_url=url,
                ),
            ),
        )
    finally:
        await client.close()


def _extract_file_id(url: str) -> str | None:
    m = _FILE_ID_RE.search(url)
    if m:
        return m.group(1)
    return None


def _extract_file_key(html: str) -> str | None:
    m = _FILE_KEY_RE.search(html)
    if m:
        return m.group(1)
    return None


def _extract_file_attr(html: str) -> str | None:
    m = _FILE_ATTR_RE.search(html)
    if m:
        return m.group(1)
    return None


def _base64_url_decode(data: str) -> bytes:
    """Decode base64url (MEGA uses base64url encoding)."""
    data = data.replace('-', '+').replace('_', '/')
    padding = '=' * (-len(data) % 4)
    return base64.b64decode(data + padding)


def _decrypt_attr(attr_b64: str, file_key_b64: str) -> str | None:
    """Decrypt MEGA file attributes."""
    try:
        attr_data = _base64_url_decode(attr_b64)
        file_key = _base64_url_decode(file_key_b64)

        # File key is 128 bits (16 bytes) for AES-128
        if len(file_key) != 16:
            return None

        # First 4 bytes are nonce, rest is ciphertext
        if len(attr_data) < 4:
            return None

        nonce = attr_data[:4]
        ciphertext = attr_data[4:]

        # MEGA uses AES-CTR with 8-byte nonce (4 bytes from file + 4 bytes counter)
        # This is a simplified version
        cipher = Cipher(algorithms.AES(file_key), modes.CTR(nonce + b'\x00\x00\x00\x00'))
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        # Attributes start with 0x01, then JSON
        if plaintext and plaintext[0] == 0x01:
            import json
            attr_json = plaintext[1:].decode('utf-8', errors='ignore')
            attrs = json.loads(attr_json)
            return attrs.get('n', 'mega_file')
        return None
    except Exception as e:
        log.debug("MEGA attribute decryption failed: %s", e)
        return None


async def _get_download_url(
    client: requests.AsyncSession, file_id: str, file_key_b64: str
) -> str | None:
    """Request download URL from MEGA API."""
    try:
        # MEGA API request
        api_url = "https://g.api.mega.nz/cs"
        params = {"id": "1"}
        data = [{"a": "g", "g": 1, "p": file_id}]

        resp = await client.post(api_url, params=params, json=data)
        result = resp.json()

        if not result or not result[0].get('g'):
            return None

        file_info = result[0]['g']
        download_url = file_info.get('g')  # direct download URL
        if download_url:
            return download_url

        # If no direct URL, try to construct from at (AT) and k (key)
        at = file_info.get('at')
        if at and file_key_b64:
            # This would require full MEGA crypto implementation
            pass

        return None
    except Exception as e:
        log.debug("MEGA API request failed: %s", e)
        return None


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        title = m.group(1).strip()
        for suffix in (" - MEGA", " | MEGA"):
            title = title.replace(suffix, "")
        return title
    return None


def resolver(
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="mega", attempt=_attempt)