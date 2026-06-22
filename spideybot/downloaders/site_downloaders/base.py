import os
import re
import urllib.parse
import requests

class BaseDownloader:
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self):
        pass

    def _sanitize_filename(self, filename: str) -> str:
        # Keep letters, numbers, spaces, underscores, dots and dashes, replace everything else with underscore
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = filename.replace(' ', '_')
        filename = filename.strip('. ')
        return filename[:200]

    def _request(self, method: str, url: str, headers: dict = None, data: dict = None, json_data: dict = None, params: dict = None, timeout: int = 15, **kwargs) -> requests.Response:
        req_headers = self.DEFAULT_HEADERS.copy()
        if headers:
            req_headers.update(headers)

        response = requests.request(
            method=method,
            url=url,
            headers=req_headers,
            data=data,
            json=json_data,
            params=params,
            timeout=timeout,
            **kwargs
        )
        response.raise_for_status()
        return response

    def _download_file(self, url: str, file_path: str, headers: dict = None) -> str:
        req_headers = self.DEFAULT_HEADERS.copy()
        if headers:
            req_headers.update(headers)

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        response = requests.get(url, headers=req_headers, stream=True, timeout=3600)
        response.raise_for_status()

        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return file_path
