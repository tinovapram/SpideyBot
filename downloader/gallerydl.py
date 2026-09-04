"""gallery-dl subprocess wrapper with size-limit enforcement and progress."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys

import structlog

from utils import paths

logger = structlog.get_logger(__name__)

_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+\.\d+)%\s+of\s+(\S+)\s+at\s+(\S+)\s+ETA\s+(\S+)")
_MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".mp4", ".webp")


class GalleryDLDownloader:
    """Downloads media via the gallery-dl CLI in a subprocess."""

    def __init__(self, download_dir=None) -> None:
        self.download_dir = str(download_dir or paths.DOWNLOADS_DIR)
        self.user_config_path = str(paths.GALLERYDL_USER_CONFIG)
        self.runtime_config_path = str(paths.GALLERYDL_RUNTIME_CONFIG)
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.runtime_config_path), exist_ok=True)
        self._generate_runtime_config()

    # ── Runtime config generation ──────────────────────────────────

    def _generate_runtime_config(self) -> None:
        browser_name = os.getenv("GDL_COOKIES_FROM_BROWSER", "").strip()

        extractors = ["twitter", "instagram", "reddit", "tiktok", "facebook", "pinterest"]
        extractor_config: dict = {}
        for name in extractors:
            entry: dict = {"metadata": True}
            if browser_name:
                entry["cookies-from-browser"] = browser_name
            extractor_config[name] = entry

        extractor_config["ytdl"] = {
            "enabled": True,
            "module": "yt_dlp",
            "config-file": self._ensure_ytdlp_config(),
        }

        reddit = self._reddit_credentials()
        if reddit:
            extractor_config["reddit"] = {**extractor_config.get("reddit", {}), **reddit}

        try:
            with open(self.runtime_config_path, "w", encoding="utf-8") as handle:
                json.dump({"extractor": extractor_config}, handle, indent=4)
        except Exception as exc:
            logger.error("Failed to write runtime gallery-dl config", error=str(exc))

    def _ensure_ytdlp_config(self) -> str:
        yt_config = str(paths.YTDLP_CONFIG)
        if not os.path.exists(yt_config):
            try:
                open(yt_config, "w", encoding="utf-8").close()
            except Exception as exc:
                logger.error("Failed to create yt-dlp config", path=yt_config, error=str(exc))
        return yt_config

    @staticmethod
    def _reddit_credentials() -> dict:
        client_id = (os.getenv("GDL_REDDIT_CLIENT_ID") or os.getenv("REDDIT_GDL_CLIENT_ID") or "").strip()
        secret = (os.getenv("GDL_REDDIT_CLIENT_SECRET") or os.getenv("REDDIT_GDL_CLIENT_SECRET") or "").strip()
        refresh = (os.getenv("GDL_REDDIT_REFRESH_TOKEN") or os.getenv("REDDIT_GDL_REFRESH_TOKEN") or "").strip()
        if not (client_id or secret or refresh):
            return {}
        result: dict = {}
        if client_id:
            result["client-id"] = client_id
        if secret:
            result["client-secret"] = secret
        if refresh:
            result["refresh-token"] = refresh
            result.setdefault("user-agent", "SpideyBot")
        return result

    # ── Download ───────────────────────────────────────────────────

    async def download(
        self,
        url: str,
        task_id: str,
        max_size_bytes: float,
        progress_callback=None,
    ) -> list[str]:
        dest_dir = os.path.join(self.download_dir, task_id)
        os.makedirs(dest_dir, exist_ok=True)

        cmd = self._base_command()
        try:
            process = await self._spawn([*cmd, "--destination", dest_dir, "--no-mtime", "--write-metadata", url])
        except FileNotFoundError:
            process = await self._spawn(
                [sys.executable, "-m", "gallery_dl", *cmd[1:], "--destination", dest_dir, "--no-mtime", "--write-metadata", url]
            )

        exceeded = False

        async def monitor_size() -> None:
            nonlocal exceeded
            while process.returncode is None:
                if self._dir_size(dest_dir) > max_size_bytes:
                    exceeded = True
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    break
                await asyncio.sleep(1.0)

        monitor = asyncio.create_task(monitor_size())
        downloaded_count = 0
        video_progress = ""

        try:
            while True:
                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                match = _PROGRESS_RE.search(line)
                if match:
                    pct, size, speed, eta = match.groups()
                    video_progress = f"\n• Video: {pct}% of {size} ({speed}, ETA {eta})"
                elif line.lower().endswith(_MEDIA_EXTS):
                    downloaded_count += 1

                if progress_callback:
                    text = f"📥 **SpideyBot: Downloading...**\n• Files downloaded: {downloaded_count}"
                    if video_progress:
                        text += video_progress
                    await progress_callback(text)
        finally:
            monitor.cancel()
            await process.wait()

        if exceeded:
            shutil.rmtree(dest_dir, ignore_errors=True)
            limit_mb = max_size_bytes / (1024 * 1024)
            raise ValueError(f"Download size limit of {limit_mb:.1f} MB exceeded.")

        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode("utf-8", errors="ignore").strip()
            shutil.rmtree(dest_dir, ignore_errors=True)
            if "Unsupported" in stderr or "No extractor" in stderr:
                raise ValueError("The link is not supported by gallery-dl.")
            raise RuntimeError(f"gallery-dl failed: {stderr or 'unknown error'}")

        return [
            os.path.join(dirpath, filename)
            for dirpath, _, filenames in os.walk(dest_dir)
            for filename in filenames
        ]

    # ── Internal helpers ───────────────────────────────────────────

    def _base_command(self) -> list[str]:
        cmd = ["gallery-dl"]
        if os.path.exists(self.user_config_path):
            cmd += ["--config", self.user_config_path]
        cmd += ["--config", self.runtime_config_path]
        return cmd

    @staticmethod
    async def _spawn(cmd):
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @staticmethod
    def _dir_size(path: str) -> int:
        total = 0
        for dirpath, _, filenames in os.walk(path):
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                try:
                    if os.path.exists(full):
                        total += os.path.getsize(full)
                except OSError:
                    pass
        return total
