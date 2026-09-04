import os

from ..base import BaseDownloader, find_url


class KuaishouDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://kuaishouvideodownloader.net/",
        }
        resp = self._request(
            "POST",
            "https://kuaishouvideodownloader.net/api/fetch-video-info",
            headers=headers,
            json_data={"videoUrl": url},
        )
        return resp.json()

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        download_url = (
            info.get("download_url")
            or info.get("url")
            or info.get("videoUrl")
            or info.get("video")
        )
        title = info.get("title") or "Kuaishou_Video"

        if not download_url:
            download_url = find_url(info, patterns=(".mp4", "kuaishou"))

        if not download_url:
            raise ValueError(f"No download URL found in Kuaishou response: {info}")

        path = os.path.join(output_dir, f"{self._sanitize_filename(title)}.mp4")
        self._download_file(download_url, path)
        return [path]
