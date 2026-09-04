import os

from ..base import BaseDownloader, find_url


class CapCutDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Origin": "https://www.genviral.io",
            "Referer": "https://www.genviral.io/tools/download/capcut",
            "Content-Type": "application/json",
        }
        resp = self._request(
            "POST",
            "https://www.genviral.io/api/tools/social-downloader",
            headers=headers,
            json_data={"url": url},
        )
        return resp.json()

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        download_url = (
            info.get("download_url")
            or info.get("url")
            or (info.get("links") or {}).get("video")
        )
        title = info.get("title") or "CapCut_Video"

        if not download_url:
            download_url = find_url(info, patterns=(".mp4", "googleusercontent"))

        if not download_url:
            raise ValueError(f"No download URL found in CapCut response: {info}")

        path = os.path.join(output_dir, f"{self._sanitize_filename(title)}.mp4")
        self._download_file(download_url, path)
        return [path]
