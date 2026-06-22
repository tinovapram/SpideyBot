import os
from .base import BaseDownloader

class SpotifyDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://musicfab.io/",
            "Origin": "https://musicfab.io",
        }

        try:
            resp = self._request("POST", "https://musicfab.io/api/spotify", headers=headers, json_data={"url": url})
            data = resp.json()
            return data
        except Exception as e:
            raise RuntimeError(f"Failed to fetch Spotify data: {e}")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        
        # Check potential download URLs in the response
        download_url = info.get("downloadUrl") or info.get("url") or info.get("link")
        title = info.get("title") or info.get("name") or "Spotify_Track"
        
        # If it's a list/tracks
        tracks = info.get("tracks", [])
        downloaded_paths = []

        if download_url:
            safe_title = self._sanitize_filename(title)
            file_path = os.path.join(output_dir, f"{safe_title}.mp3")
            self._download_file(download_url, file_path)
            downloaded_paths.append(file_path)
        elif tracks:
            for idx, track in enumerate(tracks, 1):
                track_url = track.get("downloadUrl") or track.get("url")
                track_title = track.get("title") or f"Track_{idx}"
                if track_url:
                    safe_title = self._sanitize_filename(track_title)
                    file_path = os.path.join(output_dir, f"{safe_title}.mp3")
                    self._download_file(track_url, file_path)
                    downloaded_paths.append(file_path)
        else:
            # Try to search for any download links in the response keys
            # or raise error if none
            for key, val in info.items():
                if isinstance(val, str) and (val.startswith("http://") or val.startswith("https://")) and ".mp3" in val:
                    safe_title = self._sanitize_filename(title)
                    file_path = os.path.join(output_dir, f"{safe_title}.mp3")
                    self._download_file(val, file_path)
                    return [file_path]
            raise ValueError(f"No download links found in Spotify API response: {info}")

        return downloaded_paths
