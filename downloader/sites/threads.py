"""Threads native resolver — the lovethreads.net bridge.

Behaviour grounded in V5 `downloader/site/threads.py`. Like Douyin's bridge, this one answers with
HTML inside JSON and the media are found by *kind*: a ``.icon-dlimage`` row carries the photo
options (``select.photo-option option``, whose ``value`` is the URL) and a ``.icon-dlvideo`` row
carries the video anchor. A post can hold both, and a carousel is several of one, so this returns
*N* files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

ENGINE = "threads"
_HOSTS = ("threads.net", "threads.com")

_ENDPOINT = "https://lovethreads.net/api/ajaxSearch"
_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://lovethreads.net",
    "Referer": "https://lovethreads.net/en",
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
    async with bridge.opened(
        client, timeout=timeout, referer="https://lovethreads.net/"
    ) as session:
        await bridge.load_cookies(session, cookies_file, default_domain=".lovethreads.net")
        answer = await bridge.post_json(
            session,
            _ENDPOINT,
            data={"q": url, "t": "media", "lang": "en"},
            headers=_HEADERS,
        )

    if not isinstance(answer, dict) or answer.get("status") != "ok" or not answer.get("data"):
        raise ResolveError("Threads' bridge did not return a usable answer")

    soup = bridge.soup_of(str(answer["data"]))
    videos, photos = _media(soup)
    # Videos first, matching the previous implementation: a post that carries both is usually a
    # video with a cover, and the user asked for the post rather than for its stills.
    found = [(item, "video") for item in videos] + [(item, "photo") for item in photos]
    if not found:
        raise ResolveError("Threads' bridge returned no media for that post")

    files = []
    for index, (media_url, kind) in enumerate(found, start=1):
        extension = bridge._extension_of(media_url, fallback=".jpg" if kind == "photo" else ".mp4")
        filename = bridge.filename_for(
            "Threads_Post", extension=extension, index=index, total=len(found)
        )
        files.append(bridge.media(media_url, filename, role="file", source_url=url))
    return bridge.resolution(ENGINE, url, files)


def _media(soup: Any) -> tuple[list[str], list[str]]:
    """``(videos, photos)`` in the order the answer lists them."""
    videos: list[str] = []
    photos: list[str] = []
    for item in soup.select(".download-box > li"):
        if item.select_one(".icon-dlimage"):
            option = item.select_one(".photo-option option")
            value = option.get("value") if option else None
            if value:
                photos.append(str(value))
        if item.select_one(".icon-dlvideo"):
            anchor = item.select_one('a[title="Download Video"]')
            href = anchor.get("href") if anchor else None
            if href:
                videos.append(str(href))
    return videos, photos


def resolver(
    *, cookies_file: Path | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
