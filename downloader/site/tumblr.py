import os

from ..base import BaseDownloader, find_url


class TumblrDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://tumbleclip.com/en",
        }
        resp = self._request(
            "POST", "https://tumbleclip.com/api/tumblr", headers=headers, json_data={"url": url}
        )
        return resp.json()

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        download_url = (
            info.get("download_url")
            or info.get("url")
            or info.get("video")
            or info.get("image")
        )
        title = info.get("title") or "Tumblr_Post"

        if not download_url:
            download_url = find_url(info, patterns=(".mp4", ".jpg", ".gif"))

        if not download_url:
            raise ValueError(f"No download URL found in Tumblr response: {info}")

        ext = ".mp4" if ".mp4" in download_url else ".jpg"
        path = os.path.join(output_dir, f"{self._sanitize_filename(title)}{ext}")
        self._download_file(download_url, path)
        return [path]
