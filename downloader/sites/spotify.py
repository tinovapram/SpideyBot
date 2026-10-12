"""Spotify native resolver — the musicfab.io bridge.

Behaviour grounded in V5 `downloader/site/spotify.py`. Two answer shapes, both handled because the
endpoint uses whichever suits the link: a single track (``downloadUrl``/``url``/``link``), or an
album/playlist as ``tracks[]``. The second is why this adapter is not the shared JSON helper — it
produces *N* files rather than one, each named after its own track.

Spotify's own audio is DRM-protected, so what a bridge returns is a matched recording from
elsewhere. That is a real caveat, and it is why this sits after the engines: nothing here should be
the only route to a file the user actually wanted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

ENGINE = "spotify"
_HOSTS = ("spotify.com", "spotify.link")

_ENDPOINT = "https://musicfab.io/api/spotify"


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
        client, timeout=timeout, referer="https://musicfab.io/"
    ) as session:
        await bridge.load_cookies(session, cookies_file, default_domain=".musicfab.io")
        answer = await bridge.post_json(
            session,
            _ENDPOINT,
            json_body={"url": url},
            headers={"Origin": "https://musicfab.io"},
        )

    if not isinstance(answer, dict):
        raise ResolveError("Spotify's bridge did not answer with track data")

    tracks = _tracks(answer)
    if not tracks:
        raise ResolveError("Spotify's bridge returned no downloadable track")

    files = []
    for index, (title, media_url) in enumerate(tracks, start=1):
        filename = bridge.filename_for(
            title,
            extension=bridge._extension_of(media_url, fallback=".mp3"),
            index=index,
            total=len(tracks),
        )
        files.append(bridge.media(media_url, filename, role="audio", source_url=url))
    return bridge.resolution(ENGINE, url, files)


def _tracks(answer: dict[str, Any]) -> list[tuple[str, str]]:
    """``(title, url)`` for every track the answer carries, in the order it lists them.

    A single-track answer and a release are the same thing here: an album of one. ``files`` and the
    ``total`` in the filename are then trivially consistent, which is what keeps the numbering of a
    real album from depending on which shape the bridge chose.
    """
    direct = answer.get("downloadUrl") or answer.get("url") or answer.get("link")
    if isinstance(direct, str) and direct.startswith("http"):
        title = bridge._title_of(answer) or "Spotify_Track"
        return [(title, direct)]

    listed: list[tuple[str, str]] = []
    for position, track in enumerate(answer.get("tracks") or [], start=1):
        if not isinstance(track, dict):
            continue
        url = track.get("downloadUrl") or track.get("url")
        if isinstance(url, str) and url.startswith("http"):
            title = track.get("title") or track.get("name") or f"Track_{position}"
            listed.append((str(title), url))
    return listed


def resolver(
    *, cookies_file: Path | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
