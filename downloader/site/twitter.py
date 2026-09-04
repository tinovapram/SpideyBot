import json
import os
import re
from typing import Iterator

import structlog
from bs4 import BeautifulSoup

from ..base import BaseDownloader

logger = structlog.get_logger(__name__)


class TwitterDownloader(BaseDownloader):
    def _fetch_vxtwitter(self, url: str) -> dict:
        match = re.search(r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})", url)
        if not match:
            raise ValueError("Could not extract tweet ID from URL.")

        resp = self._request(
            "GET", f"https://api.vxtwitter.com/Twitter/status/{match.group(1)}", timeout=15
        )
        metadata = resp.json()

        media_urls = [m.get("url") for m in metadata.get("media_extended", []) if m.get("url")]
        if not media_urls:
            raise ValueError("No media URLs found in vxtwitter response.")

        return {
            "title": metadata.get("text", "Twitter Post"),
            "author": metadata.get("user_screen_name") or metadata.get("user_name") or "twitter",
            "media_urls": media_urls,
            "text": metadata.get("text"),
        }

    def _fetch_savetwitter(self, url: str) -> dict:
        payload = {"q": url, "lang": "en", "cftoken": ""}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://savetwitter.net",
            "Referer": "https://savetwitter.net/en4",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = self._request("POST", "https://savetwitter.net/api/ajaxSearch", headers=headers, data=payload)
        data = resp.json()
        if data.get("status") != "ok" or not data.get("data"):
            raise ValueError("Failed to fetch Twitter/X media")

        soup = BeautifulSoup(data["data"], "html.parser")
        title_el = soup.select_one(".tw-middle h3")
        title = title_el.text.strip() if title_el else "Twitter Post"

        videos, images = [], []
        for button in soup.select(".tw-button-dl"):
            href = button.get("href")
            text = button.text
            if not href or "dl.snapcdn.app" not in href:
                continue
            if "MP4" in text:
                videos.append(href)
            elif "图片" in text or "image" in text.lower():
                images.append(href)

        for image in soup.select(".photo-list img"):
            if image.get("src"):
                images.append(image["src"])

        media_urls = videos or images
        if not media_urls:
            raise ValueError("No download links found for Twitter/X post")

        return {"title": title, "author": "twitter", "media_urls": media_urls, "text": title}

    def fetch_media(self, url: str) -> dict:
        try:
            return self._fetch_vxtwitter(url)
        except Exception as exc:
            logger.warning("vxtwitter failed, trying savetwitter", error=str(exc))
            return self._fetch_savetwitter(url)

    @staticmethod
    def _extension(media_url: str) -> str:
        clean = media_url.split("?")[0]
        match = re.search(r"\.(\w{3,4})$", clean)
        if match:
            ext = f".{match.group(1)}"
            return ".mp4" if ext.lower() in (".mp4", ".m3u8") else ext
        return ".jpg"

    def download(self, url: str, output_dir: str = "downloads") -> list:
        return list(self.download_streaming(url, output_dir))

    def download_streaming(self, url: str, output_dir: str = "downloads") -> Iterator[str]:
        info = self.fetch_media(url)
        safe_title = self._sanitize_filename(info["title"])
        os.makedirs(output_dir, exist_ok=True)

        multiple = len(info["media_urls"]) > 1
        for index, media_url in enumerate(info["media_urls"], 1):
            ext = self._extension(media_url)
            name = f"{safe_title}_{index}{ext}" if multiple else f"{safe_title}{ext}"
            path = os.path.join(output_dir, name)
            self._download_file(media_url, path)
            yield path

        if info.get("text"):
            meta_path = os.path.join(output_dir, "metadata.json")
            try:
                with open(meta_path, "w", encoding="utf-8") as handle:
                    json.dump(
                        {"category": "twitter", "author": info["author"], "text": info["text"]},
                        handle,
                        indent=4,
                    )
                yield meta_path
            except Exception as exc:
                logger.warning("Failed to write metadata.json", error=str(exc))
