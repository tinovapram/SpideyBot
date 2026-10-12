"""Pinterest native resolver — the savepin.app bridge.

Behaviour grounded in V5 `downloader/site/pinterest.py`. The bridge renders a table of download
options, and the *first* row is the highest quality it found, which is why this takes one row rather
than all of them: a pin is one file, and offering every rendition would deliver the same image three
times.

The row's format cell says whether what it links to is a video or a still, so the extension follows
the row rather than a constant (FR-8.1). A pin with no download row still has a thumbnail, and
falling back to it is better than reporting nothing — a pin *is* its image.
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

ENGINE = "pinterest"
_HOSTS = ("pinterest.com", "pin.it", "pinterest.co.uk", "pinterest.de", "pinterest.fr")

_ENDPOINT = "https://www.savepin.app/download.php"
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Referer": "https://www.savepin.app/",
    "Upgrade-Insecure-Requests": "1",
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
    target = (
        f"{_ENDPOINT}?url={urllib.parse.quote(url)}&lang=en&type=redirect"
    )
    async with bridge.opened(
        client, timeout=timeout, referer="https://www.savepin.app/"
    ) as session:
        await bridge.load_cookies(session, cookies_file, default_domain=".savepin.app")
        html = await bridge.get_text(session, target, headers=_HEADERS)

    soup = bridge.soup_of(html)
    heading = soup.find("h1")
    title = heading.get_text(strip=True) if heading else "Pinterest_Post"

    best = _best_option(soup)
    if best is None:
        thumbnail = soup.select_one(".image-container img")
        src = thumbnail.get("src") if thumbnail is not None else None
        if not src:
            raise ResolveError("Pinterest's bridge returned no download link for that pin")
        filename = bridge.filename_for(
            title, extension=bridge._extension_of(str(src), fallback=".jpg")
        )
        return bridge.resolution(
            ENGINE, url, [bridge.media(str(src), filename, source_url=url)]
        )

    media_url, is_video = best
    fallback = ".mp4" if is_video else ".jpg"
    filename = bridge.filename_for(
        title, extension=bridge._extension_of(media_url, fallback=fallback)
    )
    return bridge.resolution(
        ENGINE, url, [bridge.media(media_url, filename, role="video" if is_video else "file",
                                  source_url=url)]
    )


def _best_option(soup: Any) -> tuple[str, bool] | None:
    """``(url, is_video)`` for the first usable row of the download table."""
    for row in soup.select("tbody tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        anchor = row.find("a")
        href = anchor.get("href") if anchor else None
        if not href:
            continue
        # The bridge routes through its own redirector, so the real URL is the `url=` parameter. A
        # link without one is already direct.
        media_url = (
            urllib.parse.unquote(href.split("url=", 1)[1]) if "url=" in href else str(href)
        )
        if not media_url.startswith("http"):
            continue
        return media_url, "video" in cells[1].get_text(strip=True).lower()
    return None


def resolver(
    *, cookies_file: Path | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
