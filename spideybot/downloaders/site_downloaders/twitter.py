import os
import re
import json

import structlog
from bs4 import BeautifulSoup
from .base import BaseDownloader

logger = structlog.get_logger(__name__)

class TwitterDownloader(BaseDownloader):
    def _fetch_vxtwitter(self, url: str) -> dict:
        tweet_ids = re.findall(r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})", url)
        if not tweet_ids:
            raise ValueError("Could not extract tweet ID from URL.")
            
        tweet_id = tweet_ids[0]
        api_url = f"https://api.vxtwitter.com/Twitter/status/{tweet_id}"
        
        resp = self._request("GET", api_url, timeout=15)
        metadata = resp.json()
        
        media_urls = []
        for media in metadata.get("media_extended", []):
            media_url = media.get("url")
            if media_url:
                media_urls.append(media_url)
                
        if not media_urls:
            raise ValueError("No media URLs found in vxtwitter response.")
            
        return {
            "title": metadata.get("text", "Twitter Post"),
            "author": metadata.get("user_screen_name") or metadata.get("user_name") or "twitter",
            "media_urls": media_urls,
            "text": metadata.get("text")
        }

    def _fetch_savetwitter(self, url: str) -> dict:
        endpoint = "https://savetwitter.net/api/ajaxSearch"
        payload = {
            "q": url,
            "lang": "en",
            "cftoken": ""
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://savetwitter.net",
            "Referer": "https://savetwitter.net/en4",
            "X-Requested-With": "XMLHttpRequest",
        }
        
        resp = self._request("POST", endpoint, headers=headers, data=payload)
        res_data = resp.json()

        if res_data.get("status") != "ok" or not res_data.get("data"):
            raise ValueError("Failed to fetch Twitter/X media")

        soup = BeautifulSoup(res_data["data"], "html.parser")

        title_el = soup.select_one(".tw-middle h3")
        title = title_el.text.strip() if title_el else "Twitter Post"

        videos = []
        images = []

        for dl_btn in soup.select(".tw-button-dl"):
            href = dl_btn.get("href")
            text = dl_btn.text

            if not href or "dl.snapcdn.app" not in href:
                continue

            if "MP4" in text:
                quality_match = re.search(r'\((\d+p)\)', text)
                quality = quality_match.group(1) if quality_match else "unknown"
                videos.append(href)
            elif "图片" in text or "image" in text.lower():
                images.append(href)

        for img in soup.select(".photo-list img"):
            src = img.get("src")
            if src:
                images.append(src)

        media_urls = videos or images
        if not media_urls:
            raise ValueError("No download links found for Twitter/X post")

        return {
            "title": title,
            "author": "twitter",
            "media_urls": media_urls,
            "text": title
        }

    def fetch_media(self, url: str) -> dict:
        # We try vxtwitter first, then fall back to savetwitter
        try:
            logger.info("Attempting Twitter download via vxtwitter API...")
            return self._fetch_vxtwitter(url)
        except Exception as e:
            logger.warning("vxtwitter failed, trying savetwitter", error=str(e))
            return self._fetch_savetwitter(url)

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        safe_title = self._sanitize_filename(info["title"])
        
        downloaded_paths = []
        os.makedirs(output_dir, exist_ok=True)

        for idx, media_url in enumerate(info["media_urls"], 1):
            # Extract file extension or default to jpg
            ext = ".jpg"
            clean_url = media_url.split("?")[0]
            match = re.search(r"\.(\w{3,4})$", clean_url)
            if match:
                ext = f".{match.group(1)}"
                if ext.lower() in [".mp4", ".m3u8"]:
                    ext = ".mp4"
            
            file_path = os.path.join(output_dir, f"{safe_title}_{idx}{ext}" if len(info["media_urls"]) > 1 else f"{safe_title}{ext}")
            self._download_file(media_url, file_path)
            downloaded_paths.append(file_path)

        # Save metadata.json for caption extraction
        if info.get("text"):
            try:
                meta_data = {
                    "category": "twitter",
                    "author": info["author"],
                    "text": info["text"]
                }
                meta_path = os.path.join(output_dir, "metadata.json")
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta_data, f, indent=4)
                downloaded_paths.append(meta_path)
            except Exception as meta_err:
                logger.warning("Failed to write metadata.json", error=str(meta_err))

        return downloaded_paths
