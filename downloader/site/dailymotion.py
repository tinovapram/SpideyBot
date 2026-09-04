import os

from ..base import BaseDownloader, find_url


class DailymotionDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {"Referer": "https://ssdown.app/dailymotion"}
        resp = self._request(
            "GET", "https://ssdown.app/api/dailymotion", headers=headers, params={"url": url}
        )
        return resp.json()

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        title = info.get("title") or "Dailymotion_Video"
        download_url = info.get("url") or info.get("download_url")

        data = info.get("data")
        if not download_url and isinstance(data, dict):
            title = data.get("title") or title
            download_url = data.get("url") or data.get("download_url") or data.get("video")
            if not download_url and data.get("links"):
                download_url = next(
                    (link.get("url") for link in data["links"] if isinstance(link, dict) and link.get("url")),
                    None,
                )

        if not download_url:
            download_url = find_url(info, patterns=(".mp4", "dailymotion", "dmcdn"))

        if not download_url:
            raise ValueError(f"No download URL found in Dailymotion response: {info}")

        path = os.path.join(output_dir, f"{self._sanitize_filename(title)}.mp4")
        self._download_file(download_url, path)
        return [path]
