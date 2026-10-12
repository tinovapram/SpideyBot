"""Kuaishou native resolver — the mobile share page's own embedded state.

The previous implementation used a third-party bridge (``kuaishouvideodownloader.net``) and that
domain is gone: it is NXDOMAIN for public resolvers, so no amount of retrying brings it back.
Kuaishou's own desktop GraphQL is not a replacement — it answers with an anti-crawl captcha
challenge (``result: 400002``) to any client without a logged-in browser session. What does work is
the mobile share page for a photo id, which embeds its state as plain JSON in the markup: the media
URLs are in there, along with the caption and the exact size the size gate needs before downloading
anything.

Two of the state's habits are traps. Booleans arrive as the *strings* ``"true"``/``"False"``, so a
truthiness check would read every representation as visible; and the top-level keys are obfuscated
(a one-character shift, ``tusjoh`` for ``string``), which is why the media is found by walking the
tree for the shape we want rather than by naming a path into it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

ENGINE = "kuaishou"
_HOSTS = ("kuaishou.com", "gifshow.com", "chenzhongtech.com")

#: The share page that carries the state. The host is deliberately the *generic* one: a share link
#: redirects to a per-request hostname (``kph1tf76.m.chenzhongtech.com``), which is the same page.
_SHARE = "https://v.m.chenzhongtech.com/fw/photo/{photo_id}"

#: The photo id as it appears in every URL form a Kuaishou link comes in.
_PHOTO_ID = re.compile(r"/(?:short-video|long-video|fw/photo|photo)/([A-Za-z0-9_-]{4,40})")

#: The share page only embeds its state for a mobile client. With a desktop user agent it answers a
#: JavaScript shell that contains no state at all (74 KB against the mobile page's 162 KB), which is
#: why this is a constant rather than the bridge's default browser agent.
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

_STATE = "INIT_STATE"
_DEFAULT_TITLE = "Kuaishou_Video"


def matches(url: str) -> bool:
    return bridge.host_matches(url, _HOSTS)


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: float = bridge.DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> Resolution:
    async with bridge.opened(client, timeout=timeout) as session:
        await bridge.load_cookies(session, cookies_file, default_domain=".kuaishou.com")

        match = _PHOTO_ID.search(url)
        if match is None:
            # A share link is nothing but a redirect; the id only exists in its destination. The
            # mobile agent matters here too: with a desktop one the redirect stops at the
            # `www.kuaishou.com/short-video/…` JavaScript page instead of the photo page.
            destination = await bridge.get_final_url(
                session, url, headers={"User-Agent": _MOBILE_UA}, timeout=timeout
            )
            match = _PHOTO_ID.search(destination)
            if match is None:
                raise ResolveError("that is not a Kuaishou video link")

        html = await bridge.get_text(
            session,
            _SHARE.format(photo_id=match.group(1)),
            headers={"User-Agent": _MOBILE_UA},
            timeout=timeout,
        )
        photo = _photo_in(html)
        media_url, filesize = _best_media(photo)
        if media_url is None:
            raise ResolveError("the share page carried no video for that post")

        filename = bridge.filename_for(_title_of(photo), extension=".mp4")
        return bridge.resolution(
            ENGINE,
            url,
            [bridge.media(media_url, filename, role="video", filesize=filesize, source_url=url)],
        )


def _photo_in(html: str) -> dict[str, Any]:
    """The state object holding the video, out of the page's embedded JSON."""
    marker = html.find(_STATE)
    if marker < 0:
        # The page answered, so this is a page-shape change rather than a transport failure; saying
        # which one it is saves the next person from debugging a working network.
        raise ResolveError("the share page carried no state (Kuaishou may have changed it)")

    starts = html.find("=", marker)
    if starts < 0:
        raise ResolveError("the share page's state is not an assignment")

    try:
        state, _ = json.JSONDecoder().raw_decode(html[starts + 1 :].lstrip())
    except ValueError as error:
        raise ResolveError(f"the share page's state is not readable JSON: {error}") from error

    for value in _objects(state):
        photo = value.get("photo")
        if isinstance(photo, dict) and isinstance(photo.get("manifest"), dict):
            return photo
    raise ResolveError("the share page's state held no photo")


def _objects(node: Any) -> Any:
    """Every dict in a JSON tree, so a caller can hunt for a shape rather than key names."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


def _best_media(photo: dict[str, Any]) -> tuple[str | None, int | None]:
    """The best URL for the video, and the size it claims, if it claims one.

    The manifest is preferred because each representation states its own bitrate and size — and the
    size is what lets the size gate refuse an oversize video before a byte is transferred (E3),
    which the mirrors cannot do. Ranked by bitrate rather than by list order, because the order is
    not documented and the encodings are mixed (a 720p AVC and a 720p HEVC share one group).
    """
    for group in _adaptation_sets(photo):
        visible = [
            rep
            for rep in group.get("representation") or []
            if isinstance(rep, dict) and rep.get("url") and not _is_true(rep.get("hidden"))
        ]
        for rep in sorted(visible, key=_bitrate, reverse=True):
            return str(rep["url"]), _as_int(rep.get("fileSize"))

    for mirror in photo.get("mainMvUrls") or []:
        if isinstance(mirror, dict) and mirror.get("url"):
            return str(mirror["url"]), None
    return None, None


def _adaptation_sets(photo: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = photo.get("manifest")
    groups = manifest.get("adaptationSet") if isinstance(manifest, dict) else None
    return [group for group in groups or [] if isinstance(group, dict)]


def _title_of(photo: dict[str, Any]) -> str:
    """The caption, else who posted it, else a generic name.

    Kuaishou captions are frequently a hashtag or empty, and a file called ``.mp4`` is worse than
    one named after its author — but a caption is what the user recognises, so it wins if present.
    """
    for key in ("caption", "userName"):
        value = photo.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return _DEFAULT_TITLE


def _is_true(value: Any) -> bool:
    """Kuaishou's state serialises booleans as strings, and ``bool("False")`` is ``True``."""
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _bitrate(rep: dict[str, Any]) -> int:
    return _as_int(rep.get("avgBitrate")) or 0


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def resolver(
    *, cookies_file: Path | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
