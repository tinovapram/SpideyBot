import os
import re
import time
import urllib.parse
import requests
from typing import Iterator

from spideybot.utils.files import sanitize_filename

class BaseDownloader:
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self):
        # Use a requests.Session for connection pooling (TCP keep-alive).
        # Each subclass instance gets its own session, which is safe for
        # single-threaded usage inside run_in_executor.
        self._session = requests.Session()
        self._session.headers.update(self.DEFAULT_HEADERS)
        self._progress_callback = None  # set externally for download progress

    def _sanitize_filename(self, filename: str) -> str:
        """Delegate to the canonical utility function."""
        return sanitize_filename(filename)

    def _request(self, method: str, url: str, headers: dict = None, data: dict = None, json_data: dict = None, params: dict = None, timeout: int = 15, **kwargs) -> requests.Response:
        req_headers = self.DEFAULT_HEADERS.copy()
        if headers:
            req_headers.update(headers)

        response = self._session.request(
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

    def _download_file(self, url: str, file_path: str, headers: dict = None, progress_callback=None) -> str:
        """Download a file with optional progress callback.

        Args:
            progress_callback: Callable ``(downloaded_bytes, total_bytes)``
                called periodically during the download.  *total_bytes* may
                be 0 if the server does not send Content-Length.
        """
        req_headers = self.DEFAULT_HEADERS.copy()
        if headers:
            req_headers.update(headers)

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        response = self._session.get(url, headers=req_headers, stream=True, timeout=3600)
        response.raise_for_status()

        total = int(response.headers.get("Content-Length", 0))
        downloaded = 0
        last_cb = 0.0

        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and (time.time() - last_cb) >= 5.0:
                        last_cb = time.time()
                        progress_callback(downloaded, total)

        return file_path

    def download_streaming(self, url: str, output_dir: str = "downloads") -> Iterator[str]:
        """Yield file paths one-by-one as they finish downloading.

        Subclasses that download files individually (e.g. gallery posts)
        should override this to yield each path immediately.  The default
        falls back to ``download()`` which collects everything first.
        """
        yield from self.download(url, output_dir=output_dir)
