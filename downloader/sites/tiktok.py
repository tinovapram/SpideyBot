"""TikTok native resolver — the ssstik.io bridge.

Behaviour grounded in V5 `downloader/site/tiktok.py`. This bridge answers with **HTML directly**
(not JSON), and the anchors say which media they are: ``a.download_link`` is the video and
``a.download_link.slide`` is a photo from a slideshow post — so the slide class is excluded from the
video scan rather than being allowed to win by coming first.

A slideshow post is *only* images, and they are returned in order; a video post is one file. That
is the whole difference between the two branches, and getting it wrong would either deliver a
slideshow's first frame as a "video" or lose the other slides.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

ENGINE = "tiktok"
_HOSTS = ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com")

_ENDPOINT = "https://ssstik.io/abc?url=dl"
#: The endpoint's own page token. Rotating it is the site's prerogative (R4).
_PAGE_TOKEN = "dHl6Ylg4"
_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": "https://ssstik.io/en-1",
    "hx-request": "true",
    "hx-target": "target",
    "hx-trigger": "_gcaptcha_pt",
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
    async with bridge.opened(client, timeout=timeout, referer="https://ssstik.io/") as session:
        await bridge.load_cookies(session, cookies_file, default_domain=".ssstik.io")
        html = await bridge.post_text(
            session,
            _ENDPOINT,
            data={"id": url, "locale": "en", "tt": _PAGE_TOKEN},
            headers=_HEADERS,
        )

    soup = bridge.soup_of(html)
    title = _title(soup)
    slides, video = _links(soup)

    if slides:
        # A photo post: every slide, in order, so the user gets the carousel rather than one frame.
        files = [
            bridge.media(
                slide,
                bridge.filename_for(
                    title, extension=".jpg", index=index, total=len(slides)
                ),
                source_url=url,
            )
            for index, slide in enumerate(slides, start=1)
        ]
        return bridge.resolution(ENGINE, url, files)

    if video:
        filename = bridge.filename_for(title, extension=".mp4")
        return bridge.resolution(
            ENGINE, url, [bridge.media(video, filename, role="video", source_url=url)]
        )

    raise ResolveError("TikTok's bridge returned no download link")


def _title(soup: Any) -> str:
    node = soup.select_one("#avatar_and_text h2") or soup.select_one("#avatarAndTextUsual h2")
    return node.get_text(strip=True) if node else "TikTok_Post"


def _links(soup: Any) -> tuple[list[str], str | None]:
    """``(slides, video)`` — the photo post's images, and the video when there is one."""
    video: str | None = None
    slides: list[str] = []

    for anchor in soup.select("a.download_link"):
        classes = anchor.get("class") or []
        href = anchor.get("href")
        if not href or href == "#":
            continue
        if "slide" in classes:
            slides.append(str(href))
        elif video is None:
            # The first non-slide link, which is the watermark-free file the bridge lists first.
            video = str(href)

    return slides, video


def resolver(
    *, cookies_file: Path | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
