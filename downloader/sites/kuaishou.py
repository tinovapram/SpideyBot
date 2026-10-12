"""Kuaishou native resolver — the kuaishouvideodownloader.net bridge.

Behaviour grounded in V5 `downloader/site/kuaishou.py`: POST the link as ``videoUrl`` and read the
media URL out of the answer (``download_url``, ``url``, ``videoUrl`` or ``video`` — the endpoint has
used all four).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from downloader.contracts import Resolution
from downloader.flow import Resolver
from downloader.sites import bridge

ENGINE = "kuaishou"
_HOSTS = ("kuaishou.com", "gifshow.com", "chenzhongtech.com")

_ENDPOINT = "https://kuaishouvideodownloader.net/api/fetch-video-info"


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
        default_title="Kuaishou_Video",
        payload={"videoUrl": url},
        referer="https://kuaishouvideodownloader.net/",
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
