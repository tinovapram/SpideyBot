"""Cyberdrop-DL subprocess wrapper for Cyberdrop, Bunkr, Cyberfile, etc.

Wraps the ``cyberdrop-dl`` CLI (``pip install cyberdrop-dl-patched``).
Only downloads — no interactive TUI.  Produces a minimal runtime YAML
config that disables subfolders, sorting, and history so each invocation
is self-contained.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from typing import Iterator

import structlog

from ..base import BaseDownloader
from utils import paths

logger = structlog.get_logger(__name__)

# Domains that CDL's own Cyberdrop/Bunkr crawlers handle natively.
# We register the primary ones; CDL also covers mirrors dynamically.
_CYBERDROP_HOSTS = ("cyberdrop.me", "cyberdrop.to", "cyberdrop.cr")
_BUNKR_HOSTS = ("bunkrr.su", "bunkr.su", "bunkr.is", "bunkr.la", "bunkr.se", "bunkr.cr")
_CYBERFILE_HOSTS = ("cyberfile.me",)
_VIDARA_HOSTS = ("igbsa.lol",)

_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".mp4", ".webp", ".mkv"}

# Regex that matches CDL download progress lines:
#   [1/5] downloading file.mp4 ...
_DL_RE = re.compile(r"\[(\d+)/(\d+)\]\s+downloading\s+", re.IGNORECASE)


class CyberdropDLDownloader(BaseDownloader):
    """Download from Cyberdrop/Bunkr/Cyberfile via the ``cyberdrop-dl`` CLI."""

    def __init__(self) -> None:
        super().__init__()
        self._config_dir = str(paths.CONFIG_DIR / "cyberdrop-dl")
        self._config_path = os.path.join(self._config_dir, "config.yaml")
        self._cache_path = os.path.join(self._config_dir, "cache.json")
        self._db_path = os.path.join(self._config_dir, "cyberdrop.db")
        os.makedirs(self._config_dir, exist_ok=True)
        self._ensure_config()

    # ── URL matching ──────────────────────────────────────────────

    @classmethod
    def matches(cls, url: str) -> bool:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return any(
            host == h or host.endswith("." + h)
            for h in _CYBERDROP_HOSTS + _BUNKR_HOSTS + _CYBERFILE_HOSTS + _VIDARA_HOSTS
        )

    # ── Config bootstrap ──────────────────────────────────────────

    def _ensure_config(self) -> None:
        """Write a minimal YAML config if absent."""
        if os.path.exists(self._config_path):
            return
        # Minimal config: disable interactive features, set reasonable defaults.
        config = (
            "download_folder: downloads\n"
            "deep_scrape: false\n"
            "delete_partial_files: true\n"
            "ignore_history: true\n"
            "ignore_hashes: true\n"
            "mtime: false\n"
            "max_thread_depth: 0\n"
            "subfolders:\n"
            "  create: false\n"
            "sort:\n"
            "  enabled: false\n"
            "hashing:\n"
            "  enabled: off\n"
        )
        try:
            with open(self._config_path, "w", encoding="utf-8") as f:
                f.write(config)
        except Exception as exc:
            logger.error("Failed to write cyberdrop-dl config", error=str(exc))

    # ── Download ──────────────────────────────────────────────────

    def download(self, url: str, output_dir: str = "downloads") -> list[str]:
        """Synchronous download — blocks until complete."""
        return asyncio.get_event_loop().run_until_complete(
            self.download_async(url, output_dir)
        )

    async def download_async(self, url: str, output_dir: str) -> list[str]:
        os.makedirs(output_dir, exist_ok=True)
        cmd = self._build_command(url, output_dir)
        logger.info("Running cyberdrop-dl", url=url, dest=output_dir)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore").strip()
            logger.warning("cyberdrop-dl exited non-zero", code=process.returncode, stderr=err[:500])
            raise RuntimeError(f"cyberdrop-dl failed (exit {process.returncode}): {err[:300]}")

        return self._collect_files(output_dir)

    async def download_streaming(self, url: str, output_dir: str) -> Iterator[str]:
        """Yield downloaded files one at a time as they appear on disk."""
        os.makedirs(output_dir, exist_ok=True)
        cmd = self._build_command(url, output_dir)

        # Snapshot existing files before launch.
        before = set(self._walk_files(output_dir))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Poll for new files while process runs.
        yielded = set()
        while process.returncode is None:
            await asyncio.sleep(1.0)
            after = set(self._walk_files(output_dir))
            new_files = after - before - yielded
            for f in sorted(new_files):
                yielded.add(f)
                yield f

        # Drain remaining.
        await process.wait()
        after = set(self._walk_files(output_dir))
        for f in sorted(after - before - yielded):
            yield f

        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"cyberdrop-dl failed: {stderr[:300]}")

    # ── Helpers ───────────────────────────────────────────────────

    def _build_command(self, url: str, output_dir: str) -> list[str]:
        return [
            sys.executable, "-m", "cyberdrop_dl",
            "download", url,
            "--download-folder", output_dir,
            "--config-file", self._config_path,
            "--no-subfolders",
            "--no-mtime",
            "--no-stats",
            "--ignore-history",
            "--ignore-hashes",
            "--delete-partial-files",
            "--no-sort",
            "--hashing", "off",
            "--ui", "disabled",
        ]

    @staticmethod
    def _collect_files(directory: str) -> list[str]:
        return [
            os.path.join(dp, fn)
            for dp, _, fns in os.walk(directory)
            for fn in fns
            if not fn.endswith((".part", ".cdl_hls", ".tmp", ".json", ".csv", ".log"))
        ]

    @staticmethod
    def _walk_files(directory: str) -> list[str]:
        """Flat list of all files under *directory*."""
        if not os.path.isdir(directory):
            return []
        result = []
        for dp, _, fns in os.walk(directory):
            for fn in fns:
                if not fn.endswith((".part", ".cdl_hls", ".tmp")):
                    result.append(os.path.join(dp, fn))
        return result
