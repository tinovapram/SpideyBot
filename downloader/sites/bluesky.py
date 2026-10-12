"""Bluesky native resolver — the bskysaver.com bridge.

Behaviour grounded in V5 `downloader/site/bluesky.py`. The bridge answers with a rendered page whose
``section.download_result_section`` holds one ``.download_item`` per attachment: an image row
exposes its file through the row's *action button* (the ``img`` is a thumbnail, not the media),
and a video row exposes it as the ``video`` element's ``src``.

Videos win when a post carries both, as in the previous implementation: a Bluesky post that has both
is a video with a still attached, and that is what the user is asking for.
"""

from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

ENGINE = "bluesky"
_HOSTS = ("bsky.app", "bsky.social")

_ENDPOINT = "https://bskysaver.com/download"
_HEADERS = {"Referer": "https://bskysaver.com/"}


def matches(url: str) -> bool:
    return bridge.host_matches(url, _HOSTS)


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: float = bridge.DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> Resolution:
    target = f"{_ENDPOINT}?url={urllib.parse.quote(url)}"
    async with bridge.opened(
        client, timeout=timeout, referer="https://bskysaver.com/"
    ) as session:
        await bridge.load_cookies(session, cookies_file, default_domain=".bskysaver.com")
        html = await bridge.get_text(session, target, headers=_HEADERS)

    soup = bridge.soup_of(html)
    section = soup.select_one("section.download_result_section")
    if section is None:
        raise ResolveError("Bluesky's bridge returned no result section for that post")

    title = _caption(section) or "Bluesky_Post"
    videos, photos = _attachments(section)
    found = [(item, ".mp4") for item in videos] + [(item, None) for item in photos]
    if not found:
        raise ResolveError("Bluesky's bridge returned no media for that post")

    files = []
    for index, (media_url, fallback) in enumerate(found, start=1):
        extension = bridge._extension_of(media_url, fallback=fallback or ".jpg")
        filename = bridge.filename_for(
            title, extension=extension, index=index, total=len(found)
        )
        files.append(bridge.media(media_url, filename, source_url=url))
    return bridge.resolution(ENGINE, url, files)


def _caption(section: Any) -> str | None:
    node = section.select_one(".download__item__caption__text")
    return node.get_text(strip=True) if node else None


def _attachments(section: Any) -> tuple[list[str], list[str]]:
    """``(videos, photos)`` for the post, in the order the rows appear."""
    videos: list[str] = []
    photos: list[str] = []

    for item in section.select(".download_item"):
        video = item.select_one(".video_wrapper video")
        if video is not None and video.get("src"):
            videos.append(str(video["src"]))
            continue

        image = item.select_one(".image_wrapper img")
        if image is None:
            continue
        button = item.select_one("a.download__item__info__actions__button")
        href = button.get("href") if button else None
        if href:
            photos.append(str(href))

    return videos, photos


def resolver(
    *, cookies_file: Path | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
