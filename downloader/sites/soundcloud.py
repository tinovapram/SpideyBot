"""SoundCloud native resolver — the urlmp4.com bridge.

Behaviour grounded in V5 `downloader/site/soundcloud.py`: POST the track link to
``urlmp4.com/wp-json/aio-dl/video-data/`` as a form body, and read the download URL out of the
answer, which puts it either at the top level or in ``medias[]``.

The ``token`` and ``hash`` fields are the endpoint's own CSRF-style pair, copied from the page the
previous implementation scraped. They are a standing breakage surface (R4): the site can rotate
either, and then this adapter fails and the chain moves on — which is exactly the behaviour a
third-party bridge should have.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution
from downloader.flow import Resolver
from downloader.sites import bridge

ENGINE = "soundcloud"
_HOSTS = ("soundcloud.com", "snd.sc", "soundcloud.app")

_ENDPOINT = "https://urlmp4.com/wp-json/aio-dl/video-data/"
_TOKEN = "8b6e170975d92939bb67d8db567f82e43fa2da91e00a84f258af77c1186c5e8a"
_HASH = "aHR0cHM6Ly9zb3VuZGNsb3VkLmNvbS9zb21icnNvbmdzL3VuZHJlc3NlZA%3D%3D1043YWlvLWRs"


def matches(url: str) -> bool:
    return bridge.host_matches(url, _HOSTS)


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: float = bridge.DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> Resolution:
    body = urllib.parse.urlencode({"url": url, "token": _TOKEN, "hash": _HASH})
    return await bridge.json_bridge(
        engine=ENGINE,
        source_url=url,
        endpoint=_ENDPOINT,
        default_title="SoundCloud_Track",
        # A track, so the fallback name is audio: the answer's own URL decides in practice, and this
        # only applies when it has no usable extension at all.
        extension=".mp3",
        payload=body,
        form=True,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "pll_language=en",
            "Referer": "https://urlmp4.com/en/soundcloud-downloader/",
        },
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
