import os
from .base import BaseDownloader

class KuaishouDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://kuaishouvideodownloader.net/",
        }

        try:
            resp = self._request("POST", "https://kuaishouvideodownloader.net/api/fetch-video-info", headers=headers, json_data={"videoUrl": url})
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to fetch Kuaishou data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        
        # Check standard Kuaishou API response keys
        download_url = info.get("download_url") or info.get("url") or info.get("videoUrl") or info.get("video")
        title = info.get("title") or "Kuaishou_Video"

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
                elif isinstance(d, str) and (d.startswith("http://") or d.startswith("https://")) and (".mp4" in d or "kuaishou" in d):
                    return d
                return None
            
            download_url = find_url(info)

        if not download_url:
            raise ValueError(f"No download URL found in Kuaishou response: {info}")

        safe_title = self._sanitize_filename(title)
        file_path = os.path.join(output_dir, f"{safe_title}.mp4")
        self._download_file(download_url, file_path)
        return [file_path]
