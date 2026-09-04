import os
import urllib.parse

from bs4 import BeautifulSoup

from ..base import BaseDownloader


class PinterestDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        encoded = urllib.parse.quote(url)
        full_url = f"https://www.savepin.app/download.php?url={encoded}&lang=en&type=redirect"
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Referer": "https://www.savepin.app/",
            "Upgrade-Insecure-Requests": "1",
        }

        resp = self._request("GET", full_url, headers=headers)
        soup = BeautifulSoup(resp.text, "html.parser")

        h1 = soup.find("h1")
        title = h1.text.strip() if h1 else "Pinterest Post"

        img = soup.select_one(".image-container img")
        thumbnail = img["src"] if img and img.has_attr("src") else None

        downloads = []
        for row in soup.select("tbody tr"):
            quality_el = row.select_one(".video-quality")
            quality = quality_el.text.strip() if quality_el else ""
            tds = row.select("td")
            format_type = tds[1].text.strip() if len(tds) > 1 else ""
            link_el = row.find("a")
            href = link_el.get("href") if link_el else ""
            direct = urllib.parse.unquote(href.split("url=")[1]) if "url=" in href else href

            if quality and format_type and direct:
                downloads.append({"quality": quality, "format": format_type, "url": direct})

        return {"title": title, "thumbnail": thumbnail, "downloads": downloads}

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        safe_title = self._sanitize_filename(info["title"])

        # Native pin caption/title first so flow.py attaches it to the file.
        results: list = []
        meta_path = self._write_metadata(
            output_dir, {"category": "pinterest", "title": info["title"]}
        )
        if meta_path:
            results.append(meta_path)

        if not info["downloads"]:
            if info["thumbnail"]:
                path = os.path.join(output_dir, f"{safe_title}.jpg")
                self._download_file(info["thumbnail"], path)
                results.append(path)
                return results
            raise ValueError("No download links found for Pinterest")

        best = info["downloads"][0]
        ext = ".mp4" if "video" in best["format"].lower() else ".jpg"
        path = os.path.join(output_dir, f"{safe_title}{ext}")
        self._download_file(best["url"], path)
        results.append(path)
        return results
