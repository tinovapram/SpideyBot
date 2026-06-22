import os
import urllib.parse
from bs4 import BeautifulSoup
from .base import BaseDownloader

class PinterestDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        encoded_url = urllib.parse.quote(url)
        full_url = f"https://www.savepin.app/download.php?url={encoded_url}&lang=en&type=redirect"

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Referer": "https://www.savepin.app/",
            "Upgrade-Insecure-Requests": "1"
        }

        try:
            resp = self._request("GET", full_url, headers=headers)
            html = resp.text
            soup = BeautifulSoup(html, "html.parser")

            h1_el = soup.find("h1")
            title = h1_el.text.strip() if h1_el else "Pinterest Post"

            img_el = soup.select_one(".image-container img")
            thumbnail = img_el["src"] if img_el and img_el.has_attr("src") else None

            downloads = []
            for row in soup.select("tbody tr"):
                quality_el = row.select_one(".video-quality")
                quality = quality_el.text.strip() if quality_el else ""
                
                tds = row.select("td")
                format_type = tds[1].text.strip() if len(tds) > 1 else ""
                
                link_el = row.find("a")
                href = link_el.get("href") if link_el else ""
                
                if "url=" in href:
                    direct_url = urllib.parse.unquote(href.split("url=")[1])
                else:
                    direct_url = href

                if quality and format_type and direct_url:
                    downloads.append({
                        "quality": quality,
                        "format": format_type,
                        "url": direct_url
                    })

            return {
                "title": title,
                "thumbnail": thumbnail,
                "downloads": downloads
            }
        except Exception as e:
            raise RuntimeError(f"Failed to fetch Pinterest data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        title = self._sanitize_filename(info["title"])

        if not info["downloads"]:
            # Maybe it's a direct image pin? Let's check if the thumbnail is the only image, or try to fallback
            if info["thumbnail"]:
                ext = ".jpg"
                file_path = os.path.join(output_dir, f"{title}{ext}")
                self._download_file(info["thumbnail"], file_path)
                return [file_path]
            raise ValueError("No download links found for Pinterest")

        # Take the best quality option
        best_option = info["downloads"][0]
        ext = ".mp4" if "video" in best_option["format"].lower() else ".jpg"
        file_path = os.path.join(output_dir, f"{title}{ext}")
        
        self._download_file(best_option["url"], file_path)
        return [file_path]
