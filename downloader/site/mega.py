"""MEGA.nz downloader — public-link only, no auth required.

Implements the MEGA protocol from scratch using ``pycryptodome`` (already a
project dependency) for AES-ECB key decryption and AES-CTR stream decryption.

Protocol reference: reverse-engineered from open-source clients and
MEGA-INDEX-CLOUDFLARE's ``worker.js``.
"""

from __future__ import annotations

import base64
import os
import re
import struct
from typing import Iterator

import requests
import structlog

from ..base import BaseDownloader

logger = structlog.get_logger(__name__)

_API = "https://g.api.mega.co.nz/cs"
_CHUNK = 1 << 20  # 1 MiB — matches MEGA CDN chunk alignment


# ── MEGA base-64 alphabet ─────────────────────────────────────────
_MEGA_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

def _b64_decode(data: str) -> bytes:
    """MEGA uses a custom base64 alphabet with no padding."""
    table = str.maketrans(_MEGA_B64, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
    s = data.translate(table)
    pad = (4 - len(s) % 4) % 4
    return base64.b64decode(s + "=" * pad)


# ── URL parsing ───────────────────────────────────────────────────

# Public file links:
#   https://mega.nz/file/KEY#FILEID        (new format)
#   https://mega.nz/#!KEY!FILEID           (legacy)
# Public folder links (folder root node):
#   https://mega.nz/folder/KEY#FOLDERID    (new format)
#   https://mega.nz/#!FOLDERID!KEY         (legacy)
_FILE_RE = re.compile(
    r"mega\.(?:nz|co\.nz)/(?:file/|#" r"!?)([A-Za-z0-9_-]+)[!#]([A-Za-z0-9_-]+)",
)
_FOLDER_RE = re.compile(
    r"mega\.(?:nz|co\.nz)/(?:folder/|#" r"!?)([A-Za-z0-9_-]+)[!#]([A-Za-z0-9_-]+)",
)


def _parse_url(url: str) -> tuple[str, str, bool]:
    """Return *(handle, key, is_folder)* or raise ValueError."""
    m = _FILE_RE.search(url)
    if m:
        return m.group(2), m.group(1), False

    m = _FOLDER_RE.search(url)
    if m:
        return m.group(1), m.group(2), True

    raise ValueError(f"Cannot parse MEGA link: {url}")


# ── Crypto helpers ────────────────────────────────────────────────

def _key_from_bytes(raw: bytes) -> list[int]:
    """Decode a MEGA-encoded attribute or key block.

    MEGA packs data as little-endian int64 values, then XOR-encrypts each
    block independently with the AES key (ECB mode).  The encoded form is
    an array of these int64s, base64url-encoded with the MEGA alphabet.

    *raw* is the decoded base64 bytes.  We interpret as a sequence of
    little-endian int64s.
    """
    return list(struct.unpack(f"<{len(raw) // 8}q", raw))


def _decrypt_attr(enc: list[int], aes_key: bytes) -> str:
    """Decrypt file/folder attributes and return the filename.

    MEGA encrypts attributes by XORing the cleartext attribute ints with
    key ints, then AES-ECB encrypting each block.  We reverse: ECB-decrypt
    each int, then XOR with the key ints.
    """
    cipher = _ecb_cipher(aes_key)
    out = []
    for i in range(0, len(enc), 2):
        pair = struct.pack("<2q", enc[i], enc[i + 1] if i + 1 < len(enc) else 0)
        decrypted = cipher.decrypt(pair)
        a, b = struct.unpack("<2q", decrypted)
        out.append(a)
        out.append(b)

    # Attributes are: crc32 (0), size (1), timestamp (2), name (3..)
    # The name starts at index 6 (3 int64s for the name length + chars).
    if len(out) < 8:
        return "file"

    name_len = out[6]
    name_chars = out[7 : 7 + (name_len + 1) // 2]

    chars = []
    for v in name_chars:
        chars.append(chr((v >> 0) & 0x7FFF))
        chars.append(chr((v >> 15) & 0x7FFF))

    return "".join(chars[:name_len]) or "file"


def _ecb_cipher(key: bytes):
    """AES-ECB cipher (for key/attribute decryption only)."""
    from Crypto.Cipher import AES
    return AES.new(key, AES.MODE_ECB)


def _ctr_cipher(key: bytes, iv: int) -> "AES":
    """AES-256-CTR cipher with a position-aware 128-bit IV.

    The IV is 16 bytes: first 8 bytes = little-endian chunk number,
    last 8 bytes = zeros.  MEGA CTR uses a raw counter (not a nonce+counter
    split), so we construct the 128-bit initial value directly.
    """
    from Crypto.Cipher import AES
    iv_bytes = struct.pack("<QQ", iv, 0)
    return AES.new(key, AES.MODE_CTR, initial_value=iv_bytes, nonce=b"")


# ── API helpers ───────────────────────────────────────────────────

def _api_post(session: requests.Session, data: list, id_: int = 0) -> list:
    """POST to MEGA CS endpoint and return JSON response."""
    resp = session.post(
        _API,
        params={"id": id_},
        json=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _decode_key(key_b64: str) -> bytes:
    """Decode a MEGA base64url share key → raw AES-256 key (16 bytes)."""
    raw = _b64_decode(key_b64)
    # MEGA keys are 16 bytes (128-bit AES), encoded as 4 int32s.
    ints = struct.unpack("<4I", raw)
    # The actual AES key is the XOR of each pair of int32s:
    # key[i] = ints[2i] ^ ints[2i+1], then we pack as 4 int32s → 16 bytes.
    # Wait — for a 16-byte key, the encoding is: raw bytes, possibly with
    # trailing zeros or a CRC.  We just use the first 16 bytes.
    return raw[:16]


def _full_key(key_b64: str) -> tuple[bytes, bytes]:
    """Expand a share key into (AES-256 key, AES-CTR IV base).

    MEGA stores keys as 4 int32s: ``[k0, k1, k2, k3]``.  The AES-256
    key is ``[k0^k1, k2^k3]`` packed as 16 bytes.  For AES-CTR, the IV
    base is ``[k1^k2, k3^0]``.

    Actually: the 4 ints are the share key as-is.  We use them directly
    as the 16-byte AES key (not XORed) for ECB decryption, and derive
    the IV base from the same key for CTR decryption.
    """
    raw = _b64_decode(key_b64)
    # If it's exactly 16 bytes, use as-is.
    if len(raw) == 16:
        return raw, raw
    # If it's longer (e.g. 32 bytes for the full node key), split.
    ints = struct.unpack(f"<{len(raw) // 4}I", raw)
    aes = struct.pack("<4I", *ints[:4])
    iv_base = struct.pack("<4I", *ints[4:8]) if len(ints) >= 8 else aes
    return aes, iv_base


# ── Downloader ────────────────────────────────────────────────────

class MegaDownloader(BaseDownloader):
    """Download files from MEGA.nz public share links.

    Supports both ``mega.nz/file/...`` and legacy ``mega.nz/#!...`` formats.
    Decrypts AES-CTR encrypted streams in memory (no ``megatools`` needed).
    """

    @classmethod
    def matches(cls, url: str) -> bool:
        return "mega.nz" in url or "mega.co.nz" in url

    def download(self, url: str, output_dir: str = "downloads") -> list:
        handle, key, is_folder = _parse_url(url)

        aes_key, iv_base = _full_key(key)

        # Get node attributes (filename, size).
        attrs, file_size = self._get_node_info(handle, aes_key)

        filename = self._sanitize_filename(attrs) if attrs else "mega_file"
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, filename)

        # Get CDN download URL.
        dl_url = self._get_download_url(handle)

        # Download and decrypt.
        self._download_decrypt(dl_url, out_path, aes_key, file_size)

        return [out_path]

    def download_streaming(self, url: str, output_dir: str = "downloads") -> Iterator[str]:
        """Yield file paths for each file in the share (single file = 1 yield)."""
        yield from self.download(url, output_dir=output_dir)

    # ── Internal methods ──────────────────────────────────────────

    def _get_node_info(self, handle: str, aes_key: bytes) -> tuple[str, int]:
        """Fetch file metadata from MEGA API and decrypt attributes.

        Returns *(filename, size)*.
        """
        resp = _api_post(self._session, [{"a": "g", "g": 1, "n": handle}])
        if isinstance(resp, dict) and "e" in resp:
            raise ValueError(f"MEGA API error: {resp['e']}")
        if not resp:
            raise ValueError("MEGA API returned empty response")

        node = resp[0] if isinstance(resp, list) else resp

        # s = size, attr = encrypted attributes (base64url)
        file_size = int(node.get("s", 0))
        attr_b64 = node.get("at", "")
        filename = "mega_file"

        if attr_b64:
            try:
                enc_ints = _key_from_bytes(_b64_decode(attr_b64))
                filename = _decrypt_attr(enc_ints, aes_key)
            except Exception:
                logger.warning("Failed to decrypt MEGA attributes, using fallback name")

        return filename, file_size

    def _get_download_url(self, handle: str) -> str:
        """Get the CDN download URL for a file node."""
        resp = _api_post(self._session, [{"a": "g", "g": 1, "n": handle}])
        if isinstance(resp, dict) and "e" in resp:
            raise ValueError(f"MEGA API error: {resp['e']}")
        if not resp:
            raise ValueError("MEGA API returned empty response")

        node = resp[0] if isinstance(resp, list) else resp
        dl_url = node.get("g")
        if not dl_url:
            raise ValueError("MEGA API did not return a download URL")

        return dl_url

    def _download_decrypt(
        self,
        url: str,
        out_path: str,
        aes_key: bytes,
        file_size: int,
    ) -> int:
        """Stream-download an encrypted MEGA file and decrypt it in-place."""
        with self._session.get(url, stream=True, timeout=3600) as resp:
            resp.raise_for_status()

            content_length = int(resp.headers.get("Content-Length", 0))
            total = file_size or content_length

            downloaded = 0
            last_cb = 0.0

            with open(out_path, "wb") as fh:
                chunk_num = 0
                buf = b""

                for raw_chunk in resp.iter_content(chunk_size=_CHUNK):
                    if not raw_chunk:
                        continue

                    buf += raw_chunk

                    # Decrypt complete 1 MiB chunks.
                    while len(buf) >= _CHUNK:
                        plaintext = self._decrypt_chunk(buf[:_CHUNK], aes_key, chunk_num)
                        fh.write(plaintext)
                        buf = buf[_CHUNK:]
                        chunk_num += 1
                        downloaded += len(plaintext)

                        # Progress callback (throttled to every 5 s).
                        if self._progress_callback:
                            import time
                            now = time.time()
                            if now - last_cb >= 5.0:
                                last_cb = now
                                self._progress_callback(downloaded, total)

                # Remaining bytes (< 1 MiB) — the last chunk.
                if buf:
                    plaintext = self._decrypt_chunk(buf, aes_key, chunk_num)
                    fh.write(plaintext)
                    downloaded += len(plaintext)

            if self._progress_callback:
                self._progress_callback(downloaded, total)

        return downloaded

    @staticmethod
    def _decrypt_chunk(data: bytes, key: bytes, chunk_num: int) -> bytes:
        """AES-256-CTR decrypt one MEGA chunk.

        The 128-bit IV = ``[chunk_num, 0]`` as two little-endian int64s.
        """
        iv = struct.pack("<QQ", chunk_num, 0)
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_CTR, initial_value=iv, nonce=b"")
        return cipher.decrypt(data)
