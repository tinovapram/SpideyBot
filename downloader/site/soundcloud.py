import os
import urllib.parse

from ..base import BaseDownloader, find_url


class SoundCloudDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "pll_language=en",
            "Referer": "https://urlmp4.com/en/soundcloud-downloader/",
        }
        token = "8b6e170975d92939bb67d8db567f82e43fa2da91e00a84f258af77c1186c5e8a"
        hash_val = "aHR0cHM6Ly9zb3VuZGNsb3VkLmNvbS9zb21icnNvbmdzL3VuZHJlc3NlZA%3D%3D1043YWlvLWRs"
        body = f"url={urllib.parse.quote(url)}&token={token}&hash={hash_val}"

        resp = self._request(
            "POST",
            "https://urlmp4.com/wp-json/aio-dl/video-data/",
            headers=headers,
            data=body,
        )
        return resp.json()

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        medias = info.get("medias") or [{}]
        download_url = info.get("url") or info.get("download_url") or medias[0].get("url")
        title = info.get("title") or "SoundCloud_Track"

        if not download_url and medias:
            download_url = next((m.get("url") for m in medias if m.get("url")), None)

        if not download_url:
            download_url = find_url(info, patterns=(".mp3", ".m4a", "soundcloud"))

        if not download_url:
            raise ValueError(f"No download URL found in SoundCloud response: {info}")

        path = os.path.join(output_dir, f"{self._sanitize_filename(title)}.mp3")
        self._download_file(download_url, path)
        return [path]
