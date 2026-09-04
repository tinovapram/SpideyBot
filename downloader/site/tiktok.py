import os
import re

from bs4 import BeautifulSoup

from ..base import BaseDownloader


class TikTokDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        payload = {"id": url, "locale": "en", "tt": "dHl6Ylg4"}
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://ssstik.io/en-1",
            "hx-request": "true",
            "hx-target": "target",
            "hx-trigger": "_gcaptcha_pt",
        }

        resp = self._request("POST", "https://ssstik.io/abc?url=dl", headers=headers, data=payload)
        soup = BeautifulSoup(resp.text, "html.parser")

        avatar_box = soup.select_one("#avatar_and_text h2") or soup.select_one("#avatarAndTextUsual h2")
        title = avatar_box.text.strip() if avatar_box else "TikTok Post"

        author_img = soup.select_one(".result_author")
        thumbnail = author_img["src"] if author_img and author_img.has_attr("src") else None

        downloads = []
        for link in soup.select("a.download_link"):
            if "slide" not in link.get("class", []):
                href = link.get("href")
                if href and href != "#":
                    downloads.append({
                        "type": "video",
                        "label": re.sub(r"\s+", " ", link.text).strip(),
                        "url": href,
                    })

        for link in soup.select("a.download_link.slide"):
            href = link.get("href")
            if href and href != "#":
                downloads.append({"type": "image", "url": href})

        return {"title": title, "thumbnail": thumbnail, "downloads": downloads}

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        safe_title = self._sanitize_filename(info["title"])

        images = [d for d in info["downloads"] if d["type"] == "image"]
        videos = [d for d in info["downloads"] if d["type"] == "video"]

        if images:
            paths = []
            for index, image in enumerate(images, 1):
                path = os.path.join(output_dir, f"{safe_title}_{index}.jpg")
                self._download_file(image["url"], path)
                paths.append(path)
            return paths

        if videos:
            path = os.path.join(output_dir, f"{safe_title}.mp4")
            self._download_file(videos[0]["url"], path)
            return [path]

        raise ValueError("No download links found for TikTok")
