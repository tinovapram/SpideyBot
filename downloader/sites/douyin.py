"""Douyin native resolver — the tikvideo.app bridge.

Behaviour grounded in V5 `downloader/site/douyin.py`. The bridge answers with **HTML inside JSON**
(``{"status": "ok", "data": "<html>…"}``) rather than with a URL, so this adapter parses the
fragment: the download buttons (``a.tik-button-dl``) and, when there is no button, the inline
player's ``#vid[data-src]``.

The "profile" buttons are filtered out because the same markup carries a link to the *author's*
page, which is not media — taking the first ``href`` blindly would download an avatar at best and a
web page at worst.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

ENGINE = "douyin"
_HOSTS = ("douyin.com", "iesdouyin.com")

_ENDPOINT = "https://tikvideo.app/api/ajaxSearch"
_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://tikvideo.app/en/download-douyin-video",
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
        client, timeout=timeout, referer="https://tikvideo.app/"
    ) as session:
        await bridge.load_cookies(session, cookies_file, default_domain=".tikvideo.app")
        answer = await bridge.post_json(
            session,
            _ENDPOINT,
            data={"q": url, "lang": "en", "cftoken": ""},
            headers=_HEADERS,
        )

    fragment = _fragment_from(answer)
    soup = bridge.soup_of(fragment)

    title = _text(soup, ".tik-left .thumbnail .content h3") or "Douyin_Video"
    media_url = _download_url(soup) or _attribute(soup, "#vid", "data-src")
    if not media_url:
        raise ResolveError("Douyin's bridge returned no download link")

    filename = bridge.filename_for(
        title, extension=bridge._extension_of(media_url, fallback=".mp4")
    )
    return bridge.resolution(
        ENGINE, url, [bridge.media(media_url, filename, role="video", source_url=url)]
    )


def _fragment_from(answer: Any) -> str:
    """The HTML fragment the bridge wraps in JSON, or a failure that says which half was missing."""
    if not isinstance(answer, dict) or answer.get("status") != "ok" or not answer.get("data"):
        raise ResolveError("Douyin's bridge did not return a usable answer")
    return str(answer["data"])


def _download_url(soup: Any) -> str | None:
    for button in soup.select(".tik-right .dl-action a.tik-button-dl"):
        href = button.get("href")
        if not href:
            continue
        if "profile" in button.get_text(strip=True).lower():
            continue
        return str(href)
    return None


def _text(soup: Any, selector: str) -> str | None:
    node = soup.select_one(selector)
    return node.get_text(strip=True) if node else None


def _attribute(soup: Any, selector: str, name: str) -> str | None:
    node = soup.select_one(selector)
    return str(node[name]) if node and node.has_attr(name) else None


def resolver(
    *, cookies_file: Path | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
