import os
from bs4 import BeautifulSoup
from .base import BaseDownloader

class DouyinDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        endpoint = "https://tikvideo.app/api/ajaxSearch"
        payload = {
            "q": url,
            "lang": "en",
            "cftoken": ""
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://tikvideo.app/en/download-douyin-video",
        }

        try:
            resp = self._request("POST", endpoint, headers=headers, data=payload)
            res_data = resp.json()

            if res_data.get("status") != "ok" or not res_data.get("data"):
                raise ValueError("Tikvideo returned invalid response")

            soup = BeautifulSoup(res_data["data"], "html.parser")

            img_el = soup.select_one(".tik-left .thumbnail .image-tik img")
            thumbnail = img_el.get("src") if img_el else None

            title_el = soup.select_one(".tik-left .thumbnail .content h3")
            title = title_el.text.strip() if title_el else "Douyin Video"

            duration_el = soup.select_one(".tik-left .thumbnail .content p")
            duration = duration_el.text.strip() if duration_el else None

            links = []
            for btn in soup.select(".tik-right .dl-action a.tik-button-dl"):
                label = btn.text.strip()
                href = btn.get("href")
                if href and "profile" not in label.lower():
                    links.append({
                        "label": label,
                        "url": href
                    })

            vid_el = soup.select_one("#vid")
            preview = vid_el.get("data-src") if vid_el else None

            return {
                "title": title,
                "duration": duration,
                "thumbnail": thumbnail,
                "preview": preview,
                "links": links
            }
        except Exception as e:
            raise RuntimeError(f"Failed to fetch Douyin data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        title = self._sanitize_filename(info["title"])
        
        # Determine download URL: take the preview or first download link
        download_url = None
        if info["links"]:
            download_url = info["links"][0]["url"]
        elif info["preview"]:
            download_url = info["preview"]

        if not download_url:
            raise ValueError("No download links resolved for Douyin post")

        file_path = os.path.join(output_dir, f"{title}.mp4")
        self._download_file(download_url, file_path)
        return [file_path]
