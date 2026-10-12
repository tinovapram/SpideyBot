"""Snapchat native resolver — the solyptube.com bridge.

Behaviour grounded in V5 `downloader/site/snapchat.py`: POST the Spotlight link and read the media
URL out of the answer (``download_url``, ``url``, ``video_url`` or ``video``). The endpoint is
reached with the ``spotlight.how2shout.com`` origin because that is the page it fronts, and it
rejects the request without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from downloader.contracts import Resolution
from downloader.flow import Resolver
from downloader.sites import bridge

ENGINE = "snapchat"
_HOSTS = ("snapchat.com", "snap.com")

_ENDPOINT = "https://solyptube.com/findsnapchatvideo"
_HEADERS = {
    "Origin": "https://spotlight.how2shout.com",
    "Referer": "https://spotlight.how2shout.com/",
}


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
        default_title="Snapchat_Video",
        payload={"url": url},
        headers=_HEADERS,
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
