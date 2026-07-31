import os
import urllib.parse
from bs4 import BeautifulSoup
from .base import BaseDownloader

class BlueskyDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        encoded_url = urllib.parse.quote(url)
        full_url = f"https://bskysaver.com/download?url={encoded_url}"

        headers = {
            "Referer": "https://bskysaver.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        }

        try:
            resp = self._request("GET", full_url, headers=headers)
            html = resp.text
            soup = BeautifulSoup(html, "html.parser")

            section = soup.select_one("section.download_result_section")
            if not section:
                raise ValueError("No download results section found on bskysaver")

            caption_el = section.select_one(".download__item__caption__text")
            caption = caption_el.text.strip() if caption_el else "Bluesky Post"

            photos = []
            videos = []

            for item in section.select(".download_item"):
                # Check for image
                img_wrapper = item.select_one(".image_wrapper img")
                if img_wrapper:
                    btn_el = item.select_one("a.download__item__info__actions__button")
                    btn_url = btn_el.get("href") if btn_el else None
                    if btn_url:
                        photos.append({
                            "thumbnail": img_wrapper.get("src"),
                            "url": btn_url
                        })

                # Check for video
                video_el = item.select_one(".video_wrapper video")
                if video_el:
                    video_url = video_el.get("src")
                    if video_url:
                        videos.append({
                            "thumbnail": video_el.get("poster"),
                            "url": video_url
                        })

            return {
                "title": caption,
                "photos": photos,
                "videos": videos
            }
        except Exception as e:
            raise RuntimeError(f"Failed to fetch Bluesky data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        safe_title = self._sanitize_filename(info["title"])
        
        downloaded_paths = []

        if info["videos"]:
            for idx, vid in enumerate(info["videos"], 1):
                file_path = os.path.join(output_dir, f"{safe_title}_{idx}.mp4" if len(info["videos"]) > 1 else f"{safe_title}.mp4")
                self._download_file(vid["url"], file_path)
                downloaded_paths.append(file_path)
        elif info["photos"]:
            for idx, img in enumerate(info["photos"], 1):
                file_path = os.path.join(output_dir, f"{safe_title}_{idx}.jpg")
                self._download_file(img["url"], file_path)
                downloaded_paths.append(file_path)
        else:
            raise ValueError("No media files resolved for Bluesky post")

        return downloaded_paths
