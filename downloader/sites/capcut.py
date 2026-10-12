"""CapCut native resolver — the genviral.io bridge.

Behaviour grounded in V5 `downloader/site/capcut.py`: POST the share link to
``genviral.io/api/tools/social-downloader`` and take the URL it answers with. CapCut's own web app
serves the video through a signed, short-lived URL, so the bridge is the practical route; the
``Origin``/``Referer`` pair is required by the endpoint, not decoration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from downloader.contracts import Resolution
from downloader.flow import Resolver
from downloader.sites import bridge

ENGINE = "capcut"
_HOSTS = ("capcut.com", "capcut.cn")

_ENDPOINT = "https://www.genviral.io/api/tools/social-downloader"
_HEADERS = {
    "Origin": "https://www.genviral.io",
    "Referer": "https://www.genviral.io/tools/download/capcut",
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
        default_title="CapCut_Video",
        payload={"url": url},
        headers=_HEADERS,
        referer="https://www.genviral.io/",
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
