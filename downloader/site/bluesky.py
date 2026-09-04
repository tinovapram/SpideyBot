import os
import urllib.parse

from bs4 import BeautifulSoup

from ..base import BaseDownloader


class BlueskyDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        full_url = f"https://bskysaver.com/download?url={urllib.parse.quote(url)}"
        headers = {
            "Referer": "https://bskysaver.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "Chrome/120 Safari/537.36"
            ),
        }
        resp = self._request("GET", full_url, headers=headers)
        soup = BeautifulSoup(resp.text, "html.parser")

        section = soup.select_one("section.download_result_section")
        if not section:
            raise ValueError("No download results section found on bskysaver")

        caption_el = section.select_one(".download__item__caption__text")
        caption = caption_el.text.strip() if caption_el else "Bluesky Post"

        photos, videos = [], []
        for item in section.select(".download_item"):
            img_wrapper = item.select_one(".image_wrapper img")
            if img_wrapper:
                button = item.select_one("a.download__item__info__actions__button")
                if button and button.get("href"):
                    photos.append({"thumbnail": img_wrapper.get("src"), "url": button["href"]})

            video_el = item.select_one(".video_wrapper video")
            if video_el and video_el.get("src"):
                videos.append({"thumbnail": video_el.get("poster"), "url": video_el["src"]})

        return {"title": caption, "photos": photos, "videos": videos}

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        safe_title = self._sanitize_filename(info["title"])

        if info["videos"]:
            paths = []
            multiple = len(info["videos"]) > 1
            for index, video in enumerate(info["videos"], 1):
                name = f"{safe_title}_{index}.mp4" if multiple else f"{safe_title}.mp4"
                path = os.path.join(output_dir, name)
                self._download_file(video["url"], path)
                paths.append(path)
            return paths

        if info["photos"]:
            paths = []
            for index, photo in enumerate(info["photos"], 1):
                path = os.path.join(output_dir, f"{safe_title}_{index}.jpg")
                self._download_file(photo["url"], path)
                paths.append(path)
            return paths

        raise ValueError("No media files resolved for Bluesky post")
