import os

from ..base import BaseDownloader, find_url


class LinkedInDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://saywhat.ai/tools/linkedin-video-downloader/",
        }
        resp = self._request(
            "POST",
            "https://saywhat.ai/api/fetch-linkedin-page/",
            headers=headers,
            json_data={"url": url},
        )
        return resp.json()

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        download_url = info.get("downloadUrl") or info.get("url") or info.get("videoUrl")
        title = info.get("title") or "LinkedIn_Video"

        if not download_url:
            download_url = find_url(info, patterns=(".mp4",))

        if not download_url:
            raise ValueError(f"No download URL found in LinkedIn response: {info}")

        path = os.path.join(output_dir, f"{self._sanitize_filename(title)}.mp4")
        self._download_file(download_url, path)
        return [path]
