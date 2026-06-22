import os
from bs4 import BeautifulSoup
from .base import BaseDownloader

class ThreadsDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        endpoint = "https://lovethreads.net/api/ajaxSearch"
        payload = {
            "q": url,
            "t": "media",
            "lang": "en"
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://lovethreads.net",
            "Referer": "https://lovethreads.net/en",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            resp = self._request("POST", endpoint, headers=headers, data=payload)
            res_data = resp.json()

            if res_data.get("status") != "ok" or not res_data.get("data"):
                raise ValueError("Failed to fetch Threads media")

            soup = BeautifulSoup(res_data["data"], "html.parser")

            photos = []
            videos = []

            for li in soup.select(".download-box > li"):
                # Check for Photo
                if li.select_one(".icon-dlimage"):
                    thumb_el = li.select_one(".download-items__thumb img")
                    thumbnail = thumb_el.get("src") if thumb_el else None
                    
                    variants = []
                    for opt in li.select(".photo-option option"):
                        val = opt.get("value")
                        label = opt.text.strip()
                        if val and "x" in label:
                            variants.append({
                                "resolution": label,
                                "url": val
                            })
                    
                    # Sort variants by resolution area if possible, otherwise take the first
                    if variants:
                        photos.append({
                            "thumbnail": thumbnail,
                            "url": variants[0]["url"]
                        })

                # Check for Video
                if li.select_one(".icon-dlvideo"):
                    thumb_el = li.select_one(".download-items__thumb img")
                    thumbnail = thumb_el.get("src") if thumb_el else None
                    
                    video_el = li.select_one('a[title="Download Video"]')
                    video_url = video_el.get("href") if video_el else None

                    if video_url:
                        videos.append({
                            "thumbnail": thumbnail,
                            "url": video_url
                        })

            return {
                "title": "Threads Post",
                "photos": photos,
                "videos": videos
            }
        except Exception as e:
            raise RuntimeError(f"Failed to fetch Threads data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        title = self._sanitize_filename(info["title"])
        
        downloaded_paths = []

        if info["videos"]:
            for idx, vid in enumerate(info["videos"], 1):
                file_path = os.path.join(output_dir, f"{title}_{idx}.mp4" if len(info["videos"]) > 1 else f"{title}.mp4")
                self._download_file(vid["url"], file_path)
                downloaded_paths.append(file_path)
        elif info["photos"]:
            for idx, img in enumerate(info["photos"], 1):
                file_path = os.path.join(output_dir, f"{title}_{idx}.jpg")
                self._download_file(img["url"], file_path)
                downloaded_paths.append(file_path)
        else:
            raise ValueError("No media files resolved for Threads post")

        return downloaded_paths
