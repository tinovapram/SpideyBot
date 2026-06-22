import os
import urllib.parse
from .base import BaseDownloader

class SoundCloudDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "pll_language=en",
            "Referer": "https://urlmp4.com/en/soundcloud-downloader/",
        }

        # Payload structure copied directly from the reference JS code
        token = "8b6e170975d92939bb67d8db567f82e43fa2da91e00a84f258af77c1186c5e8a"
        hash_val = "aHR0cHM6Ly9zb3VuZGNsb3VkLmNvbS9zb21icnNvbmdzL3VuZHJlc3NlZA%3D%3D1043YWlvLWRs"
        encoded_url = urllib.parse.quote(url)
        body = f"url={encoded_url}&token={token}&hash={hash_val}"

        try:
            resp = self._request("POST", "https://urlmp4.com/wp-json/aio-dl/video-data/", headers=headers, data=body)
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to fetch SoundCloud data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        
        # Check potential AIO-DL keys
        download_url = info.get("url") or info.get("download_url") or info.get("medias", [{}])[0].get("url")
        title = info.get("title") or "SoundCloud_Track"

        if not download_url and "medias" in info:
            for media in info["medias"]:
                if media.get("url"):
                    download_url = media["url"]
                    break

        if not download_url:
            # Fallback search
            def find_url(d):
                if isinstance(d, dict):
                    for k, v in d.items():
                        res = find_url(v)
                        if res: return res
                elif isinstance(d, list):
                    for v in d:
                        res = find_url(v)
                        if res: return res
                elif isinstance(d, str) and (d.startswith("http://") or d.startswith("https://")) and (".mp3" in d or ".m4a" in d or "soundcloud" in d):
                    return d
                return None
            
            download_url = find_url(info)

        if not download_url:
            raise ValueError(f"No download URL found in SoundCloud response: {info}")

        safe_title = self._sanitize_filename(title)
        file_path = os.path.join(output_dir, f"{safe_title}.mp3")
        self._download_file(download_url, file_path)
        return [file_path]
