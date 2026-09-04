"""Reddit downloader using PRAW (and yt-dlp for video/audio merging)."""

import glob
import html
import json
import os
import urllib.parse
from typing import Iterator

import praw
import structlog

from ..base import BaseDownloader

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

logger = structlog.get_logger(__name__)


class RedditDownloader(BaseDownloader):
    """Download media from Reddit submissions via PRAW."""

    def __init__(
        self,
        client_id=None,
        client_secret=None,
        refresh_token=None,
        refresh_token_client_id=None,
        refresh_token_client_secret=None,
        user_agent="SpideyBot/1.0 (+https://github.com/SpideyBot)",
    ) -> None:
        super().__init__()
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.refresh_token_client_id = refresh_token_client_id
        self.refresh_token_client_secret = refresh_token_client_secret
        self.user_agent = user_agent
        self.reddit = None
        self._authenticate()

    # ── Authentication ─────────────────────────────────────────────

    def _candidate_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []

        if self.refresh_token and self.refresh_token_client_id:
            pairs.append((self.refresh_token_client_id, self.refresh_token_client_secret or ""))

        if self.client_id:
            pairs.append((self.client_id, self.client_secret or ""))

        for default in (
            ("icuiw6HNK21QoQ", "pzdmKpdnB_JBO0eVkL1IZoxu3vdpug"),
            ("b70jdVnmRpFmVc12GvVuAw", "yD_McmeM_yocK0IZYaBeUg8RSBsuqQ"),
            ("pdLe8qrK8y3KUpQqFMH63A", "gRE6tl_S5ahYX2I5IF3Od29yO7fusA"),
        ):
            if default not in pairs:
                pairs.append(default)

        return pairs

    def _authenticate(self) -> None:
        pairs = self._candidate_pairs()

        if self.refresh_token:
            for cid, secret in pairs:
                try:
                    reddit = praw.Reddit(
                        client_id=cid,
                        client_secret=secret,
                        refresh_token=self.refresh_token,
                        user_agent=self.user_agent,
                    )
                    reddit.auth.scopes()
                    self.reddit = reddit
                    self.client_id, self.client_secret = cid, secret
                    logger.info("Authenticated via refresh token", client_id=cid)
                    return
                except Exception as exc:
                    logger.warning("Refresh token auth failed", client_id=cid, error=str(exc))

        for cid, secret in pairs:
            try:
                response = self._session.post(
                    "https://www.reddit.com/api/v1/access_token",
                    auth=(cid, secret),
                    data={"grant_type": "client_credentials"},
                    headers={"User-Agent": self.user_agent},
                    timeout=30,
                )
                response.raise_for_status()
                token = response.json().get("access_token")
                if token:
                    self.reddit = praw.Reddit(
                        client_id=cid, client_secret=secret,
                        user_agent=self.user_agent, access_token=token,
                    )
                    self.reddit.auth.scopes()
                    self.client_id, self.client_secret = cid, secret
                    logger.info("Authenticated via client credentials", client_id=cid)
                    return
            except Exception as exc:
                logger.warning("Client-credential auth failed", client_id=cid, error=str(exc))

        raise RuntimeError("Failed to authenticate with Reddit API.")

    # ── Download ───────────────────────────────────────────────────

    def download(self, url: str, output_dir: str = "downloads") -> list:
        return list(self.download_streaming(url, output_dir))

    def download_streaming(self, url: str, output_dir: str = "downloads") -> Iterator[str]:
        os.makedirs(output_dir, exist_ok=True)

        submission = self.reddit.submission(url=url)
        title = submission.title
        upvotes = submission.ups
        safe_title = self._sanitize_filename(title)

        if getattr(submission, "is_video", False):
            yield from self._download_video(submission, url, output_dir, upvotes, safe_title)
        elif getattr(submission, "is_gallery", False) and hasattr(submission, "media_metadata"):
            for item_id, item in submission.media_metadata.items():
                if item.get("status") != "valid" or "s" not in item:
                    continue
                image_url = item["s"].get("u")
                if not image_url:
                    continue
                image_url = html.unescape(image_url)
                ext = self._extension(item, default=".jpg")
                path = os.path.join(output_dir, f"{upvotes}_{safe_title}_{item_id}{ext}")
                self._download_file(image_url, path)
                yield path
        else:
            yield from self._download_link(submission, output_dir, upvotes, safe_title)

        yield from self._save_metadata(submission, output_dir, title)

    # ── Internal helpers ───────────────────────────────────────────

    def _download_video(self, submission, url, output_dir, upvotes, safe_title) -> Iterator[str]:
        if HAS_YTDLP:
            try:
                template = os.path.join(output_dir, f"{upvotes}_{safe_title}.%(ext)s")
                with yt_dlp.YoutubeDL({
                    "outtmpl": template,
                    "format": "bestvideo+bestaudio/best",
                    "merge_output_format": "mp4",
                    "quiet": True,
                    "no_warnings": True,
                }) as ydl:
                    ydl.download([url])
                for ext in (".mp4", ".mkv"):
                    path = os.path.join(output_dir, f"{upvotes}_{safe_title}{ext}")
                    if os.path.exists(path):
                        yield path
                        return
            except Exception as exc:
                logger.warning("yt-dlp download failed, using fallback URL", error=str(exc))

        video_url = None
        if submission.media and "reddit_video" in submission.media:
            video_url = submission.media["reddit_video"].get("fallback_url")
        if not video_url:
            raise ValueError("Could not find video fallback URL in submission media.")

        path = os.path.join(output_dir, f"{upvotes}_{safe_title}.mp4")
        self._download_file(video_url, path)
        yield path

    def _download_link(self, submission, output_dir, upvotes, safe_title) -> Iterator[str]:
        post_url = submission.url
        if post_url.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
            ext = os.path.splitext(urllib.parse.urlparse(post_url).path)[1] or ".jpg"
            path = os.path.join(output_dir, f"{upvotes}_{safe_title}{ext}")
            self._download_file(post_url, path)
            yield path
            return

        if HAS_YTDLP:
            try:
                with yt_dlp.YoutubeDL({
                    "outtmpl": os.path.join(output_dir, f"{upvotes}_{safe_title}.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                }) as ydl:
                    ydl.download([post_url])
                for path in glob.glob(os.path.join(output_dir, f"{upvotes}_{safe_title}.*")):
                    yield path
                return
            except Exception as exc:
                logger.warning("yt-dlp download failed", error=str(exc))

        raise ValueError(f"Submission URL is not a supported media format: {post_url}")

    def _save_metadata(self, submission, output_dir, title) -> Iterator[str]:
        try:
            author = submission.author.name if submission.author else "reddit"
            meta = {
                "category": "reddit",
                "author": author,
                "title": title,
                "selftext": getattr(submission, "selftext", "") or "",
            }
            path = os.path.join(output_dir, "metadata.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, indent=4)
            yield path
        except Exception as exc:
            logger.warning("Failed to save metadata JSON", error=str(exc))

    @staticmethod
    def _extension(item: dict, default: str) -> str:
        mime = item.get("m", "")
        return f".{mime.split('/')[-1]}" if "/" in mime else default
