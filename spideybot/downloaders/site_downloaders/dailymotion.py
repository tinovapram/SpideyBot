import os
from .base import BaseDownloader

class DailymotionDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Referer": "https://ssdown.app/dailymotion",
        }

        try:
            resp = self._request("GET", "https://ssdown.app/api/dailymotion", headers=headers, params={"url": url})
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to fetch Dailymotion data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        
        # Check potential ssdown response keys
        download_url = info.get("url") or info.get("download_url")
        title = info.get("title") or "Dailymotion_Video"

        # If the API returned a dictionary nested under data (as ref JS says: return { success: true, data })
        if not download_url and "data" in info:
            inner_data = info["data"]
            if isinstance(inner_data, dict):
                download_url = inner_data.get("url") or inner_data.get("download_url") or inner_data.get("video")
                title = inner_data.get("title") or title
                
                # Check nested lists
                if not download_url and "links" in inner_data:
                    for link in inner_data["links"]:
                        if isinstance(link, dict) and link.get("url"):
                            download_url = link["url"]
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
                elif isinstance(d, str) and (d.startswith("http://") or d.startswith("https://")) and (".mp4" in d or "dailymotion" in d or "dmcdn" in d):
                    return d
                return None
            
            download_url = find_url(info)

        if not download_url:
            raise ValueError(f"No download URL found in Dailymotion response: {info}")

        safe_title = self._sanitize_filename(title)
        file_path = os.path.join(output_dir, f"{safe_title}.mp4")
        self._download_file(download_url, file_path)
        return [file_path]
