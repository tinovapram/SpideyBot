"""Tumblr native resolver — the tumbleclip.com bridge.

Behaviour grounded in V5 `downloader/site/tumblr.py`: POST the post link and read the media URL out
of the answer (``download_url``, ``url``, ``video`` or ``image``). A Tumblr post can be a video or a
still, so the extension comes from the URL rather than from a constant — naming an image ``.mp4``
would make delivery send a photo as a video (FR-8.1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from downloader.contracts import Resolution
from downloader.flow import Resolver
from downloader.sites import bridge

ENGINE = "tumblr"
_HOSTS = ("tumblr.com", "tumblr.co")

_ENDPOINT = "https://tumbleclip.com/api/tumblr"


def matches(url: str) -> bool:
    return bridge.host_matches(url, _HOSTS)


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: float = bridge.DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> Resolution:
    return await bridge.json_bridge(
        engine=ENGINE,
        source_url=url,
        endpoint=_ENDPOINT,
        default_title="Tumblr_Post",
        payload={"url": url},
        referer="https://tumbleclip.com/en",
        cookies_file=cookies_file,
        timeout=timeout,
        client=client,
    )


def resolver(
    *, cookies_file: Path | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
