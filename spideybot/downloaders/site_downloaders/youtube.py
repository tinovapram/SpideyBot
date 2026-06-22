import os
from .base import BaseDownloader

class YouTubeDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        payload = {
            "auth": "20250901majwlqo",
            "domain": "api-ak.vidssave.com",
            "origin": "cache",
            "link": url,
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://vidssave.com",
            "Referer": "https://vidssave.com/",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            resp = self._request("POST", "https://api.vidssave.com/api/contentsite_api/media/parse", headers=headers, data=payload)
            res_data = resp.json()
            
            if not res_data or res_data.get("status") != 1 or "data" not in res_data:
                raise ValueError("Invalid response from vidssave API")

            video_info = res_data["data"]
            videos = []
            audios = []

            for r in video_info.get("resources", []):
                item = {
                    "format": r.get("format"),
                    "quality": r.get("quality"),
                    "url": r.get("download_url"),
                    "sizeMB": round(int(r.get("size", 0)) / 1024 / 1024, 2)
                }
                if r.get("type") == "video":
                    videos.append(item)
                elif r.get("type") == "audio":
                    audios.append(item)

            return {
                "title": video_info.get("title", "YouTube Video"),
                "thumbnail": video_info.get("thumbnail"),
                "duration": video_info.get("duration"),
                "videos": videos,
                "audios": audios
            }
        except Exception as e:
            # Fallback to a generic title/url using standard yt-dlp if it failed
            try:
                import yt_dlp
                ydl_opts = {"quiet": True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    return {
                        "title": info.get("title", "YouTube Video"),
                        "thumbnail": info.get("thumbnail"),
                        "duration": info.get("duration"),
                        "videos": [{"quality": "best", "url": url, "format": "mp4", "use_ytdlp": True}],
                        "audios": []
                    }
            except Exception:
                raise RuntimeError(f"Failed to fetch YouTube data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        title = self._sanitize_filename(info["title"])
        
        # Download best quality video
        video_url = None
        use_ytdlp = False
        
        if info["videos"]:
            # sort by quality or size if possible, default to first item
            video_url = info["videos"][0]["url"]
            use_ytdlp = info["videos"][0].get("use_ytdlp", False)
            
        if not video_url:
            raise ValueError("No download links found for YouTube video")

        file_path = os.path.join(output_dir, f"{title}.mp4")
        
        if use_ytdlp:
            import yt_dlp
            ydl_opts = {
                "outtmpl": file_path,
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
                "quiet": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return [file_path]
        else:
            self._download_file(video_url, file_path)
            return [file_path]
