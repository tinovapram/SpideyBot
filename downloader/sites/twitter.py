"""Twitter/X native resolver — the vxtwitter API, with savetwitter as the fallback.

Behaviour grounded in V5 `downloader/site/twitter.py`, which used two routes for a reason: vxtwitter
is an API that answers with typed metadata (so the media URLs come out of a JSON field and the
post's text is available), and savetwitter is an HTML bridge that has to be scraped. The API is
tried first because it is the more reliable of the two, and the second route is only attempted when
the first fails — a post vxtwitter cannot see (a protected account, an age-gated reply) is one
savetwitter sometimes can.

The tweet id is extracted from the URL rather than passed through, because the API's path wants the
id alone and a share link carries a lot besides: a username, a query string, a trailing slash.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

ENGINE = "twitter"
_HOSTS = ("twitter.com", "x.com", "mobile.twitter.com")

_API = "https://api.vxtwitter.com/Twitter/status/{tweet_id}"
_BRIDGE = "https://savetwitter.net/api/ajaxSearch"
_BRIDGE_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://savetwitter.net",
    "Referer": "https://savetwitter.net/en4",
}

#: ``twitter.com/user/status/123``, ``x.com/i/web/status/123``, and the mobile host alike.
_TWEET_ID = re.compile(r"(?:twitter|x)\.com/(?:[^/]{1,15}/)?(?:web/)?status(?:es)?/(\d{1,20})")


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
        await bridge.load_cookies(session, cookies_file, default_domain=".x.com")
        # The bridge is still tried after an "empty post" verdict — it sometimes sees a protected or
        # age-gated reply the API cannot — but if it fails too, the API's verdict is the one to
        # report: "nothing in that post" is true and actionable, where "savetwitter could not
        # resolve that post" says only that a second site also had trouble.
        empty: ResolveError | None = None
        try:
            return await _through_the_api(session, url)
        except ResolveError as error:
            # Logged rather than swallowed silently: a post that only resolves the second way is a
            # signal that the API route has changed, worth seeing before it breaks outright.
            log.info("vxtwitter could not resolve %s (%s); trying savetwitter", url, error)
            if error.empty:
                empty = error

        try:
            return await _through_the_bridge(session, url)
        except ResolveError:
            if empty is not None:
                raise empty from None
            raise


async def _through_the_api(session: Any, url: str) -> Resolution:
    """vxtwitter: typed metadata, so the media URLs are fields rather than markup."""
    match = _TWEET_ID.search(url)
    if match is None:
        raise ResolveError("that is not a Twitter/X status link")

    answer = await bridge.get_json(session, _API.format(tweet_id=match.group(1)))
    if not isinstance(answer, dict):
        raise ResolveError("vxtwitter did not answer with post metadata")

    media = [
        (item["url"], str(item.get("type", "")).lower() != "photo")
        for item in answer.get("media_extended") or []
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ]
    if not media:
        # E4's shape, not a failure: the post resolved and simply holds nothing to download. A tweet
        # that is only text is a real answer, and the engines should decide what to do with it.
        raise ResolveError("no media in that post", empty=True)

    title = _text_of(answer) or "Twitter_Post"
    return _resolution(url, title, media)


async def _through_the_bridge(session: Any, url: str) -> Resolution:
    """savetwitter: the same information, scraped out of a rendered fragment."""
    answer = await bridge.post_json(
        session,
        _BRIDGE,
        data={"q": url, "lang": "en", "cftoken": ""},
        headers=_BRIDGE_HEADERS,
    )
    if not isinstance(answer, dict) or answer.get("status") != "ok" or not answer.get("data"):
        raise ResolveError("savetwitter could not resolve that post")

    soup = bridge.soup_of(str(answer["data"]))
    heading = soup.select_one(".tw-middle h3")
    title = heading.get_text(strip=True) if heading else "Twitter_Post"

    videos: list[str] = []
    images: list[str] = []
    for anchor in soup.select(".tw-button-dl"):
        href = anchor.get("href")
        # The bridge also lists "open in app" links; only its own CDN carries media.
        if not href or "dl.snapcdn.app" not in href:
            continue
        label = anchor.get_text(" ", strip=True)
        if "MP4" in label:
            videos.append(str(href))
        elif "image" in label.lower() or "图片" in label:
            images.append(str(href))
    for image in soup.select(".photo-list img"):
        if image.get("src"):
            images.append(str(image["src"]))

    # Videos before images, as in the previous implementation: a post with both is a video whose
    # stills are attached, and the video is what was asked for.
    if videos:
        return _resolution(url, title, [(item, True) for item in videos])
    if images:
        return _resolution(url, title, [(item, False) for item in images])
    raise ResolveError("no media in that post", empty=True)


def _resolution(url: str, title: str, media: list[tuple[str, bool]]) -> Resolution:
    """One file per media item, with the kind stated rather than inferred from the URL.

    The bridge's own links carry no extension at all (``…/mp4/1``), so a URL-based guess would name
    a video ``.jpg`` — and delivery would then send it as a photo (FR-8.1). The route that found the
    item already knows which it is, so it says so.
    """
    files = []
    for index, (media_url, is_video) in enumerate(media, start=1):
        fallback = ".mp4" if is_video else ".jpg"
        extension = bridge._extension_of(media_url, fallback=fallback)
        filename = bridge.filename_for(
            title, extension=extension, index=index, total=len(media)
        )
        # A playlist gets the `hls` role so the fetcher hands it to ffmpeg instead of saving it as a
        # text file named `.mp4`. Checked on the URL, because the extension has already been mapped.
        role = "hls" if ".m3u8" in media_url.lower() else "file"
        files.append(bridge.media(media_url, filename, role=role, source_url=url))
    return bridge.resolution(ENGINE, url, files)


def _text_of(answer: dict[str, Any]) -> str | None:
    text = answer.get("text")
    if isinstance(text, str) and text.strip():
        # First line only: a thread's text is long, and a filename is not the place for it.
        return text.strip().splitlines()[0][:120]
    return None


def resolver(
    *, cookies_file: Path | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
