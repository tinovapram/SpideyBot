import os

from bs4 import BeautifulSoup

from ..base import BaseDownloader


class ThreadsDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        payload = {"q": url, "t": "media", "lang": "en"}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://lovethreads.net",
            "Referer": "https://lovethreads.net/en",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = self._request("POST", "https://lovethreads.net/api/ajaxSearch", headers=headers, data=payload)
        data = resp.json()
        if data.get("status") != "ok" or not data.get("data"):
            raise ValueError("Failed to fetch Threads media")

        soup = BeautifulSoup(data["data"], "html.parser")
        photos, videos = [], []

        for li in soup.select(".download-box > li"):
            if li.select_one(".icon-dlimage"):
                options = li.select(".photo-option option")
                if options:
                    photos.append({"url": options[0].get("value")})
            if li.select_one(".icon-dlvideo"):
                video_el = li.select_one('a[title="Download Video"]')
                if video_el and video_el.get("href"):
                    videos.append({"url": video_el["href"]})

        return {"title": "Threads Post", "photos": photos, "videos": videos}

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

        raise ValueError("No media files resolved for Threads post")
