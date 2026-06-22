import os
from .base import BaseDownloader

class CapCutDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Origin": "https://www.genviral.io",
            "Referer": "https://www.genviral.io/tools/download/capcut",
            "Content-Type": "application/json",
        }

        try:
            resp = self._request("POST", "https://www.genviral.io/api/tools/social-downloader", headers=headers, json_data={"url": url})
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to fetch CapCut data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        
        # Check standard GenViral API response keys
        download_url = info.get("download_url") or info.get("url") or info.get("links", {}).get("video")
        title = info.get("title") or "CapCut_Video"

        # Fallback to searching all values for video urls
        if not download_url:
            for key, val in info.items():
                if isinstance(val, str) and (val.startswith("http://") or val.startswith("https://")) and ".mp4" in val:
                    download_url = val
                    break
        
        if not download_url:
            # Let's inspect the entire dict if there are nested dicts/lists
            def find_url(d):
                if isinstance(d, dict):
                    for k, v in d.items():
                        res = find_url(v)
                        if res: return res
                elif isinstance(d, list):
                    for v in d:
                        res = find_url(v)
                        if res: return res
                elif isinstance(d, str) and (d.startswith("http://") or d.startswith("https://")) and (".mp4" in d or "googleusercontent" in d):
                    return d
                return None
            
            download_url = find_url(info)

        if not download_url:
            raise ValueError(f"No download URL found in CapCut response: {info}")

        safe_title = self._sanitize_filename(title)
        file_path = os.path.join(output_dir, f"{safe_title}.mp4")
        self._download_file(download_url, file_path)
        return [file_path]
