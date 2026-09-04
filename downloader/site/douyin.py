import os

from bs4 import BeautifulSoup

from ..base import BaseDownloader


class DouyinDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        payload = {"q": url, "lang": "en", "cftoken": ""}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://tikvideo.app/en/download-douyin-video",
        }
        resp = self._request("POST", "https://tikvideo.app/api/ajaxSearch", headers=headers, data=payload)
        data = resp.json()
        if data.get("status") != "ok" or not data.get("data"):
            raise ValueError("Tikvideo returned invalid response")

        soup = BeautifulSoup(data["data"], "html.parser")
        img_el = soup.select_one(".tik-left .thumbnail .image-tik img")
        thumbnail = img_el.get("src") if img_el else None

        title_el = soup.select_one(".tik-left .thumbnail .content h3")
        title = title_el.text.strip() if title_el else "Douyin Video"

        links = []
        for button in soup.select(".tik-right .dl-action a.tik-button-dl"):
            href = button.get("href")
            if href and "profile" not in button.text.strip().lower():
                links.append({"label": button.text.strip(), "url": href})

        vid_el = soup.select_one("#vid")
        preview = vid_el.get("data-src") if vid_el else None

        return {"title": title, "thumbnail": thumbnail, "preview": preview, "links": links}

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        safe_title = self._sanitize_filename(info["title"])

        download_url = None
        if info["links"]:
            download_url = info["links"][0]["url"]
        elif info["preview"]:
            download_url = info["preview"]

        if not download_url:
            raise ValueError("No download links resolved for Douyin post")

        path = os.path.join(output_dir, f"{safe_title}.mp4")
        self._download_file(download_url, path)
        return [path]
