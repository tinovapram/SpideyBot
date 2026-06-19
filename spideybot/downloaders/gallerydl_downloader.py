import os
import sys
import json
import asyncio
import logging
import shutil
from typing import List

logger = logging.getLogger(__name__)

class GalleryDLDownloader:
    def __init__(self, download_dir="./downloads", user_config_path="./gallery-dl.json", runtime_config_path="./data/gallery-dl-runtime.json"):
        self.download_dir = download_dir
        self.user_config_path = user_config_path
        self.runtime_config_path = runtime_config_path
        os.makedirs(self.download_dir, exist_ok=True)
        # Ensure parent directory of runtime config exists
        os.makedirs(os.path.dirname(self.runtime_config_path), exist_ok=True)
        self._generate_runtime_config()

    def _generate_runtime_config(self):
        """Generate a fresh runtime config file based on environment variables and defaults."""
        browser_name = os.getenv("GDL_COOKIES_FROM_BROWSER", "").strip()
        
        # Build extractor configuration
        extractors = ["twitter", "instagram", "reddit", "tiktok", "facebook", "pinterest"]
        extractor_config = {}
        for ext in extractors:
            extractor_config[ext] = {"metadata": True}
            if browser_name:
                extractor_config[ext]["cookies-from-browser"] = browser_name

        # Include ytdl configuration
        extractor_config["ytdl"] = {
            "enabled": True,
            "module": "yt_dlp"
        }
        # Reference yt-dlp.conf if it exists
        if os.path.exists("./yt-dlp.conf"):
            extractor_config["ytdl"]["config-file"] = "./yt-dlp.conf"

        # Apply Reddit env credentials if present
        reddit_client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
        reddit_refresh_token = os.getenv("REDDIT_REFRESH_TOKEN", "").strip()
        if reddit_client_id or reddit_refresh_token:
            reddit = extractor_config.setdefault("reddit", {})
            if reddit_client_id:
                reddit["client-id"] = reddit_client_id
            if reddit_refresh_token:
                reddit["refresh-token"] = reddit_refresh_token
                reddit.setdefault("user-agent", "SpideyBot")

        config_data = {
            "extractor": extractor_config
        }
        
        try:
            with open(self.runtime_config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=4)
            logger.info(f"Generated runtime gallery-dl config at {self.runtime_config_path}")
        except Exception as e:
            logger.error(f"Failed to write runtime gallery-dl config: {e}")

    async def download(self, url: str, task_id: str, max_size_bytes: int, progress_callback=None) -> List[str]:
        """
        Download media from a URL using gallery-dl.
        Monitors file sizes during download to enforce the limit.
        Returns a list of downloaded file paths.
        """
        import re
        dest_dir = os.path.join(self.download_dir, task_id)
        os.makedirs(dest_dir, exist_ok=True)
        
        # Command arguments: load user config if it exists, and always load runtime config
        cmd = ["gallery-dl"]
        if os.path.exists(self.user_config_path):
            cmd.extend(["--config", self.user_config_path])
        cmd.extend(["--config", self.runtime_config_path])
        cmd.extend(["--destination", dest_dir, "--no-mtime", "--write-metadata", url])
        
        logger.info(f"Running gallery-dl command: {' '.join(cmd)}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            logger.info("gallery-dl executable not found in PATH. Falling back to python -m gallery_dl...")
            cmd = [sys.executable, "-m", "gallery_dl"]
            if os.path.exists(self.user_config_path):
                cmd.extend(["--config", self.user_config_path])
            cmd.extend(["--config", self.runtime_config_path])
            cmd.extend(["--destination", dest_dir, "--no-mtime", "--write-metadata", url])
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
            except FileNotFoundError:
                # Cleanup directory
                if os.path.exists(dest_dir):
                    shutil.rmtree(dest_dir)
                raise RuntimeError("gallery-dl CLI is not installed or not in PATH on the host system.")

        def get_dir_size(path: str) -> int:
            total_size = 0
            if not os.path.exists(path):
                return total_size
            for dirpath, _, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        if os.path.exists(fp):
                            total_size += os.path.getsize(fp)
                    except OSError:
                        pass
            return total_size

        # Monitor size while subprocess is running in a background task
        exceeded = False
        async def monitor_size():
            nonlocal exceeded
            while process.returncode is None:
                current_size = get_dir_size(dest_dir)
                if current_size > max_size_bytes:
                    logger.warning(f"Download size {current_size} bytes exceeded limit of {max_size_bytes} bytes. Terminating process.")
                    exceeded = True
                    try:
                        process.terminate()
                    except Exception as e:
                        logger.error(f"Failed to terminate gallery-dl process: {e}")
                    break
                await asyncio.sleep(1.0)

        monitor_task = asyncio.create_task(monitor_size())

        # Read stdout line-by-line in real-time to parse progress
        downloaded_count = 0
        video_progress = ""

        try:
            while True:
                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode('utf-8', errors='ignore').strip()
                if not line:
                    continue

                # Parse yt-dlp progress
                # e.g., [download]  10.5% of  15.24MiB at  2.11MiB/s ETA 00:05
                yt_match = re.search(r"\[download\]\s+(\d+\.\d+)%\s+of\s+(\S+)\s+at\s+(\S+)\s+ETA\s+(\S+)", line)
                if yt_match:
                    pct, size, speed, eta = yt_match.groups()
                    video_progress = f"\n• Video: {pct}% of {size} ({speed}, ETA {eta})"
                # Otherwise check if it looks like a downloaded file line
                # gallery-dl prints the local destination path of each successfully downloaded file
                elif any(line.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".mp4", ".webp"]):
                    downloaded_count += 1

                if progress_callback:
                    status_text = f"📥 **SpideyBot: Downloading...**\n• Files downloaded: {downloaded_count}"
                    if video_progress:
                        status_text += video_progress
                    await progress_callback(status_text)
        finally:
            monitor_task.cancel()
            await process.wait()

        # Check exit status
        if exceeded:
            if os.path.exists(dest_dir):
                shutil.rmtree(dest_dir)
            limit_mb = max_size_bytes / (1024 * 1024)
            raise ValueError(f"Download size limit of {limit_mb:.1f} MB exceeded.")
            
        if process.returncode != 0:
            stderr_data = await process.stderr.read()
            stderr_str = stderr_data.decode('utf-8', errors='ignore').strip()
            logger.error(f"gallery-dl failed with exit code {process.returncode}: {stderr_str}")
            
            if os.path.exists(dest_dir):
                shutil.rmtree(dest_dir)
                
            if "Unsupported" in stderr_str or "No extractor" in stderr_str:
                raise ValueError("The link is not supported by gallery-dl.")
            raise RuntimeError(f"gallery-dl failed: {stderr_str or 'unknown error'}")

        # Collect all downloaded files recursively
        downloaded_files = []
        for dirpath, _, filenames in os.walk(dest_dir):
            for f in filenames:
                downloaded_files.append(os.path.join(dirpath, f))
                
        return downloaded_files
