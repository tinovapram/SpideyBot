import os

from ..base import BaseDownloader


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
            resp = self._request(
                "POST",
                "https://api.vidssave.com/api/contentsite_api/media/parse",
                headers=headers,
                data=payload,
            )
            data = resp.json()
            if not data or data.get("status") != 1 or "data" not in data:
                raise ValueError("Invalid response from vidssave API")

            video_info = data["data"]
            videos, audios = [], []
            for resource in video_info.get("resources", []):
                item = {
                    "format": resource.get("format"),
                    "quality": resource.get("quality"),
                    "url": resource.get("download_url"),
                    "sizeMB": round(int(resource.get("size", 0)) / 1024 / 1024, 2),
                }
                if resource.get("type") == "video":
                    videos.append(item)
                elif resource.get("type") == "audio":
                    audios.append(item)

            return {
                "title": video_info.get("title", "YouTube Video"),
                "thumbnail": video_info.get("thumbnail"),
                "duration": video_info.get("duration"),
                "videos": videos,
                "audios": audios,
            }
        except Exception as exc:
            return self._fetch_ytdlp_fallback(url, exc)

    def _fetch_ytdlp_fallback(self, url: str, original_error: Exception) -> dict:
        import yt_dlp

        try:
            with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", "YouTube Video"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "videos": [{"quality": "best", "url": url, "format": "mp4", "use_ytdlp": True}],
                "audios": [],
            }
        except Exception as ytdlp_err:
            raise RuntimeError(
                f"Failed to fetch YouTube data: vidssave error={original_error}, "
                f"yt-dlp error={ytdlp_err}"
            ) from original_error

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        safe_title = self._sanitize_filename(info["title"])

        videos = info.get("videos") or []
        if not videos:
            raise ValueError("No download links found for YouTube video")

        video = videos[0]
        video_url = video["url"]
        file_path = os.path.join(output_dir, f"{safe_title}.mp4")

        if video.get("use_ytdlp"):
            import yt_dlp

            ydl_opts = {
                "outtmpl": file_path,
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
                "quiet": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        else:
            self._download_file(video_url, file_path)

        return [file_path]
