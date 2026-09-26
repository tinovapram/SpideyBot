"""gallery-dl downloader: resolve direct URLs via -J, then stream-download
them ourselves for accurate progress tracking and per-byte size limits."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from urllib.parse import unquote, urlparse

import aiohttp
import structlog

from utils import paths

logger = structlog.get_logger(__name__)

_CHUNK = 256 * 1024  # 256 KiB
_MAX_FILENAME_LEN = 200


class GalleryDLDownloader:
    """Downloads media by resolving gallery-dl URLs then streaming via aiohttp."""

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

    # ── Public API ─────────────────────────────────────────────────

    async def download(
        self,
        url: str,
        task_id: str,
        max_size_bytes: float,
        progress_callback=None,
    ) -> list[str]:
        dest_dir = os.path.join(self.download_dir, task_id)
        os.makedirs(dest_dir, exist_ok=True)

        # Phase 1: try -J resolve
        try:
            direct_urls, metadata_list = await self._resolve_urls(url)
            if not direct_urls:
                raise RuntimeError("gallery-dl -J returned no downloadable URLs")
        except Exception as exc:
            logger.warning("-J resolve failed, falling back to subprocess", error=str(exc))
            return await self._download_subprocess(url, dest_dir, max_size_bytes, progress_callback)

        # Phase 2: stream-download each URL ourselves
        return await self._download_urls(direct_urls, metadata_list, dest_dir, max_size_bytes, progress_callback)

    # ── Resolve phase: gallery-dl -J ───────────────────────────────

    async def _resolve_urls(self, url: str) -> tuple[list[str], list[dict]]:
        """Run ``gallery-dl -J <url>`` and extract direct download URLs + metadata."""
        cmd = self._build_resolve_cmd(url)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()

        if proc.returncode != 0:
            err = stderr_bytes.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"gallery-dl -J failed ({proc.returncode}): {err}")

        raw = stdout_bytes.decode("utf-8", errors="ignore").strip()
        if not raw:
            return [], []

        data = json.loads(raw)
        return self._extract_urls_and_meta(data)

    def _build_resolve_cmd(self, url: str) -> list[str]:
        cmd = [sys.executable, "-m", "gallery_dl", "-J"]
        if os.path.exists(self.user_config_path):
            cmd += ["--config", self.user_config_path]
        cmd += ["--config", self.runtime_config_path, url]
        return cmd

    @staticmethod
    def _extract_urls_and_meta(data) -> tuple[list[str], list[dict]]:
        """Walk the -J JSON tree and collect URLs + their metadata dicts."""
        urls: list[str] = []
        meta: list[dict] = []
        if isinstance(data, str) and data.startswith("http"):
            return [data], [{}]
        if not isinstance(data, list):
            return urls, meta
        for entry in data:
            if isinstance(entry, str) and entry.startswith("http"):
                urls.append(entry)
                meta.append({})
            elif isinstance(entry, list):
                url_found = None
                meta_found: dict = {}
                for item in entry:
                    if isinstance(item, str) and item.startswith("http"):
                        url_found = item
                    elif isinstance(item, dict) and not meta_found:
                        meta_found = item
                if url_found:
                    urls.append(url_found)
                    meta.append(meta_found)
        return urls, meta

    # ── Download phase: aiohttp streaming ──────────────────────────

    async def _download_urls(
        self,
        urls: list[str],
        metadata_list: list[dict],
        dest_dir: str,
        max_size_bytes: float,
        progress_callback=None,
    ) -> list[str]:
        total_bytes = 0
        file_paths: list[str] = []
        total_files = len(urls)
        caption_saved = False

        async with aiohttp.ClientSession() as session:
            for idx, url in enumerate(urls, 1):
                if total_bytes >= max_size_bytes:
                    break
                try:
                    path, size = await self._download_one(
                        session, url, dest_dir, idx,
                        max_size_bytes - total_bytes, progress_callback, idx, total_files,
                    )
                    total_bytes += size
                    file_paths.append(path)

                    # Save metadata sidecar on first file so extract_native_text() works.
                    meta = metadata_list[idx - 1] if idx - 1 < len(metadata_list) else {}
                    if meta and not caption_saved:
                        meta_path = path + ".json"
                        try:
                            with open(meta_path, "w", encoding="utf-8") as fh:
                                json.dump(meta, fh)
                            file_paths.append(meta_path)
                            caption_saved = True
                        except OSError as exc:
                            logger.warning("Failed to write metadata sidecar", error=str(exc))
                except Exception as exc:
                    logger.warning("Failed to download URL", url=url[:120], error=str(exc))

        return file_paths

    async def _download_one(
        self,
        session: aiohttp.ClientSession,
        url: str,
        dest_dir: str,
        idx: int,
        remaining_bytes: float,
        progress_callback,
        file_idx: int,
        total_files: int,
    ) -> tuple[str, int]:
        """Download a single URL, enforcing ``remaining_bytes`` limit."""
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            resp.raise_for_status()

            filename = self._guess_filename(resp, url, idx)
            dest_path = os.path.join(dest_dir, filename)
            if os.path.exists(dest_path):
                dest_path = self._dedupe(dest_path)

            written = 0
            with open(dest_path, "wb") as fp:
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    if written + len(chunk) > remaining_bytes:
                        fp.close()
                        os.remove(dest_path)
                        limit_mb = remaining_bytes / (1024 * 1024)
                        raise ValueError(
                            f"Download size limit of {limit_mb:.1f} MB exceeded."
                        )
                    fp.write(chunk)
                    written += len(chunk)

                    if progress_callback and total_files > 0:
                        pct = (written / max(remaining_bytes, 1)) * 100
                        dl_mb = written / (1024 * 1024)
                        text = (
                            f"📥 **SpideyBot: Downloading...**\n"
                            f"• Files: {file_idx}/{total_files}\n"
                            f"• {dl_mb:.1f} MB ({pct:.0f}%)"
                        )
                        await progress_callback(text)

        return dest_path, written

    # ── Fallback: subprocess gallery-dl ────────────────────────────

    async def _download_subprocess(
        self,
        url: str,
        dest_dir: str,
        max_size_bytes: float,
        progress_callback=None,
    ) -> list[str]:
        """Legacy subprocess path used when -J resolve fails."""
        _PROGRESS_RE = re.compile(
            r"\[download\]\s+(\d+\.\d+)%\s+of\s+(\S+)\s+at\s+(\S+)\s+ETA\s+(\S+)"
        )
        _URL_RE = re.compile(r"^https?://\S+")

        base = ["gallery-dl"]
        if os.path.exists(self.user_config_path):
            base += ["--config", self.user_config_path]
        base += ["--config", self.runtime_config_path]

        try:
            process = await asyncio.create_subprocess_exec(
                *base, "--destination", dest_dir, "--no-mtime", "--write-metadata", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "gallery_dl",
                *base[1:], "--destination", dest_dir, "--no-mtime", "--write-metadata", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        exceeded = False

        async def monitor_size():
            nonlocal exceeded
            while process.returncode is None:
                if _dir_size(dest_dir) > max_size_bytes:
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
        current_file = ""

        try:
            while True:
                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                url_match = _URL_RE.match(line)
                match = _PROGRESS_RE.search(line)
                if match:
                    pct, size, speed, eta = match.groups()
                    video_progress = f"\n• Video: {pct}% of {size} ({speed}, ETA {eta})"
                elif url_match:
                    downloaded_count += 1
                    current_file = url_match.group().split("?")[0].rsplit("/", 1)[-1]
                    video_progress = ""

                if progress_callback:
                    short = current_file if len(current_file) <= 40 else current_file[:37] + "..."
                    text = f"📥 **SpideyBot: Downloading...**\n• Files: {downloaded_count}"
                    if short:
                        text += f"\n• Current: `{short}`"
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
            os.path.join(dp, fn)
            for dp, _, fns in os.walk(dest_dir)
            for fn in fns
        ]

    # ── Filename helpers ───────────────────────────────────────────

    @staticmethod
    def _guess_filename(resp, url: str, idx: int) -> str:
        cd = resp.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            name = cd.split("filename=", 1)[-1].strip('" ')
            if name:
                return GalleryDLDownloader._sanitize(name)

        path = urlparse(url).path
        name = unquote(path.rsplit("/", 1)[-1]) or f"download_{idx}"
        return GalleryDLDownloader._sanitize(name)[:_MAX_FILENAME_LEN]

    @staticmethod
    def _sanitize(name: str) -> str:
        return re.sub(r'[\\/:*?"<>|]', "_", name)

    @staticmethod
    def _dedupe(path: str) -> str:
        base, ext = os.path.splitext(path)
        counter = 1
        while os.path.exists(path):
            path = f"{base}_{counter}{ext}"
            counter += 1
        return path


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total
