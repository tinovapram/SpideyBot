"""Dailymotion native resolver — the ssdown.app bridge.

Behaviour grounded in V5 `downloader/site/dailymotion.py`: ask ``ssdown.app/api/dailymotion`` for
the link and read the URL out of the answer, whose shape has varied (a flat ``url``, a nested
``data`` object, or a ``links`` array) — which is why the hunt is by *shape of the value* rather
than by a single key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from downloader.contracts import Resolution
from downloader.flow import Resolver
from downloader.sites import bridge

ENGINE = "dailymotion"
_HOSTS = ("dailymotion.com", "dai.ly")

_ENDPOINT = "https://ssdown.app/api/dailymotion"


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
        endpoint=f"{_ENDPOINT}?url={url}",
        default_title="Dailymotion_Video",
        method="GET",
        referer="https://ssdown.app/dailymotion",
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
