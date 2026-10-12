"""LinkedIn native resolver — the saywhat.ai bridge.

Behaviour grounded in V5 `downloader/site/linkedin.py`: POST the post URL and read the media URL out
of the answer (``downloadUrl``, ``url`` or ``videoUrl``). LinkedIn requires a session to see a post,
and the bridge does not fix that — a private post still fails, which is the honest outcome rather
than a login attempt on the user's behalf.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from downloader.contracts import Resolution
from downloader.flow import Resolver
from downloader.sites import bridge

ENGINE = "linkedin"
_HOSTS = ("linkedin.com", "lnkd.in")

_ENDPOINT = "https://saywhat.ai/api/fetch-linkedin-page/"


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
        default_title="LinkedIn_Video",
        payload={"url": url},
        referer="https://saywhat.ai/tools/linkedin-video-downloader/",
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
