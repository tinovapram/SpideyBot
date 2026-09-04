import os

from ..base import BaseDownloader


class SpotifyDownloader(BaseDownloader):
    def fetch_media(self, url: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://musicfab.io/",
            "Origin": "https://musicfab.io",
        }
        resp = self._request(
            "POST", "https://musicfab.io/api/spotify", headers=headers, json_data={"url": url}
        )
        return resp.json()

    def download(self, url: str, output_dir: str = "downloads") -> list:
        info = self.fetch_media(url)
        title = info.get("title") or info.get("name") or "Spotify_Track"

        direct = info.get("downloadUrl") or info.get("url") or info.get("link")
        if direct:
            path = os.path.join(output_dir, f"{self._sanitize_filename(title)}.mp3")
            self._download_file(direct, path)
            return [path]

        tracks = info.get("tracks", [])
        if tracks:
            paths = []
            for index, track in enumerate(tracks, 1):
                track_url = track.get("downloadUrl") or track.get("url")
                if not track_url:
                    continue
                track_title = track.get("title") or f"Track_{index}"
                path = os.path.join(output_dir, f"{self._sanitize_filename(track_title)}.mp3")
                self._download_file(track_url, path)
                paths.append(path)
            if paths:
                return paths

        raise ValueError(f"No download links found in Spotify API response: {info}")
