"""Reddit native resolver — PRAW for the post, yt-dlp for a video's merge.

Behaviour grounded in V5 `downloader/site/reddit.py`. Reddit is the one host here that needs an
identity before it will answer at all, and the chain is V5's: a configured refresh token (an
account-level login) → configured client credentials → Reddit's **public installed-app pairs**.
Those pairs are what make this adapter work on a checkout with no Reddit app registered; they are
application identities rather than secrets, and each is *validated with a real request* so a dead
one falls through to the next instead of surfacing as "Reddit would not answer".

Four kinds of post, all handled because they need different routes:

* **A video** is served as a DASH stream (video and audio apart) whose ``fallback_url`` is
  video-only. yt-dlp is handed the *post URL* so it can resolve and merge both — using the fallback
  URL alone would deliver a silent video, which is the bug worth naming here.
* **A gallery** is a list of ``media_metadata`` entries, each valid only when its ``status`` says
  so.
* **A direct image link** is its own URL.
* **Anything else** (a link post to a host we do not know) goes to yt-dlp as well, because guessing
  from an extension would fail on the hosts that matter.

Credentials arrive through :attr:`downloader.registry.ChainContext.options` — the seam declared for
exactly this — so the registry still never reads configuration.
"""

from __future__ import annotations

import asyncio
import html
import logging
import urllib.parse
from pathlib import Path
from typing import Any

from downloader.contracts import Resolution, ResolveError
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

ENGINE = "reddit"
_HOSTS = ("reddit.com", "redd.it", "redditmedia.com")

#: Reddit's public "installed app" pairs, taken from the previous implementation. They are shared
#: application identities rather than secrets: they identify the *client*, and every unauthenticated
#: Reddit client uses one. Tried after any configured credentials, so an operator's own identity
#: always wins, and each attempt is logged with the id it used so a refused pair can be spotted.
#:
#: Measured 2026-10 against the live API: the first two authenticate, the third answers 401. Kept
#: because a dead last candidate costs one failed request only when everything before it has already
#: failed, and Reddit may bring it back.
_FALLBACK_APPS: tuple[tuple[str, str], ...] = (
    ("icuiw6HNK21QoQ", "pzdmKpdnB_JBO0eVkL1IZoxu3vdpug"),
    ("b70jdVnmRpFmVc12GvVuAw", "yD_McmeM_yocK0IZYaBeUg8RSBsuqQ"),
    ("pdLe8qrK8y3KUpQqFMH63A", "gRE6tl_S5ahYX2I5IF3Od29yO7fusA"),
)

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def matches(url: str) -> bool:
    return bridge.host_matches(url, _HOSTS)


async def attempt(
    url: str,
    *,
    options: dict[str, Any] | None = None,
    timeout: float = bridge.DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> Resolution:
    """Resolve a submission into direct transfers.

    ``client`` is accepted for signature parity with the other adapters and is unused: PRAW owns its
    own transport, so there is no session of ours to inject. A test stubs :func:`_submission`.

    The authentication happens in a worker thread because it is a *blocking* HTTP request — PRAW is
    synchronous, and now that a candidate is validated eagerly (see :func:`_client`) that request
    would otherwise run on the event loop thread, stalling every other job in the worker.
    """
    reddit = await asyncio.to_thread(_client, options or {}, timeout)
    submission = await _submission(reddit, url)
    return await _files_of(submission, url)


async def _submission(reddit: Any, url: str) -> Any:
    """The submission behind a link, with PRAW's call in one place so a test can stub it."""

    def fetch() -> Any:
        return reddit.submission(url=url)

    # PRAW is synchronous, so it is run off the loop rather than blocking the worker's other jobs.
    try:
        return await asyncio.to_thread(fetch)
    except Exception as error:  # noqa: BLE001 - PRAW's message is the useful one
        raise ResolveError(f"Reddit would not answer for that link: {error}") from error


async def _files_of(submission: Any, url: str) -> Resolution:
    title = _attribute(submission, "title") or "Reddit_Post"

    if _attribute(submission, "is_video"):
        return await _video(submission, url, title)

    metadata = _attribute(submission, "media_metadata")
    if _attribute(submission, "is_gallery") and isinstance(metadata, dict) and metadata:
        files = []
        for item_id, item in metadata.items():
            if not isinstance(item, dict) or item.get("status") != "valid":
                continue
            source = (item.get("s") or {}).get("u")
            if not source:
                continue
            # No numbering: the entry's own id already makes the name unique within the album, and
            # `Gallery_a_1.jpg` would read as a version of something rather than as one of a set.
            files.append(
                bridge.media(
                    html.unescape(str(source)),
                    bridge.filename_for(f"{title}_{item_id}", extension=_gallery_extension(item)),
                    source_url=url,
                )
            )
        return bridge.resolution(ENGINE, url, files)

    return await _link(submission, url, title)


async def _video(submission: Any, url: str, title: str) -> Resolution:
    """A video post: yt-dlp resolves the DASH pair, and its absence is reported honestly.

    The fallback URL is used only when the library cannot run at all, and the caveat is carried on
    the file itself through ``note`` — because a silent video is a worse outcome than a stated one.
    """
    try:
        import yt_dlp  # noqa: F401

        from downloader import probe

        resolution = await probe.resolve_ytdlp(url, timeout=probe.DEFAULT_RESOLVE_TIMEOUT)
        if resolution.files:
            return resolution
        raise ResolveError("yt-dlp found no streams in that Reddit video")
    except ResolveError:
        raise
    except Exception as error:  # noqa: BLE001 - the library's absence or failure
        log.info("yt-dlp could not resolve the Reddit video (%s); using the fallback URL", error)

    fallback = ((_attribute(submission, "media") or {}).get("reddit_video") or {}).get(
        "fallback_url"
    )
    if not fallback:
        raise ResolveError("Reddit's post has no downloadable video URL")

    filename = bridge.filename_for(title, extension=".mp4")
    file = bridge.media(
        str(fallback),
        filename,
        role="video",
        source_url=url,
        note="without sound: Reddit serves this video's audio as a separate stream",
    )
    return bridge.resolution(ENGINE, url, [file])


async def _link(submission: Any, url: str, title: str) -> Resolution:
    """An image link is its own URL; anything else is handed to yt-dlp by URL."""
    post_url = str(_attribute(submission, "url") or "")
    suffix = Path(urllib.parse.urlparse(post_url).path).suffix.lower()

    if suffix in _IMAGE_SUFFIXES:
        filename = bridge.filename_for(title, extension=suffix)
        return bridge.resolution(
            ENGINE, url, [bridge.media(post_url, filename, source_url=url)]
        )

    try:
        from downloader import probe

        resolution = await probe.resolve_ytdlp(post_url, timeout=probe.DEFAULT_RESOLVE_TIMEOUT)
    except Exception as error:  # noqa: BLE001 - reported as this post having nothing
        raise ResolveError(f"that Reddit link holds nothing downloadable: {error}") from error
    if not resolution.files:
        raise ResolveError("that Reddit link holds nothing downloadable")
    return resolution


def _client(options: dict[str, Any], timeout: float) -> Any:
    """A PRAW client from the first identity that a request actually accepts.

    The chain is V5's, and the *validation* is the part that makes it a chain at all: PRAW
    constructs a client without contacting Reddit, so a bad pair only fails on the first API call.
    Skipping the check would mean the first candidate always "wins" and a dead identity is never
    noticed — the user would get "Reddit would not answer" instead of the next pair being tried.

    Two routes, in V5's order:

    1. **Refresh token**, when one is configured. It is an account-level login, so it is tried
       first: it is what an operator gives us if they want their own identity and its rate limit.
    2. **Application-only**, which is what Reddit's public "installed app" pairs can do. Those
       pairs are application identities rather than secrets — every unauthenticated Reddit client
       uses one — and are shared, so they are rate-limited harder than anything you register
       yourself.

    Measured 2026-10 against the live API: the first two pairs authenticate (scopes ``{'*'}``) and
    the third answers **401**. It is kept as the last candidate because a dead pair costs one failed
    request *only* when the earlier ones have already failed, and Reddit may re-enable it; the
    measurement is recorded here so the next person does not have to repeat it. Every failure is
    logged with the client id that failed, because "Reddit said no" is useless on its own and the
    interesting question is always *which* identity was refused.
    """
    try:
        import praw
    except ImportError as error:  # pragma: no cover - declared in requirements.txt
        raise ResolveError("praw is not installed, so Reddit links cannot be resolved") from error

    pairs = _candidate_pairs(options)
    user_agent = str(options.get("user_agent") or "SpideyBot/1.0 (+https://github.com/SpideyBot)")
    refresh_token = options.get("refresh_token")

    if refresh_token:
        for client_id, client_secret in pairs:
            try:
                client = praw.Reddit(
                    client_id=client_id,
                    client_secret=client_secret,
                    refresh_token=str(refresh_token),
                    user_agent=user_agent,
                    timeout=timeout,
                )
                client.auth.scopes()
                log.info("resolved with Reddit via refresh token, client id %s", client_id)
                return client
            except Exception as error:  # noqa: BLE001 - try the next identity
                log.warning("Reddit refused the refresh token for %s: %s", client_id, error)

    for client_id, client_secret in pairs:
        try:
            client = praw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                user_agent=user_agent,
                timeout=timeout,
            )
            client.read_only = True
            # The one call that proves the pair works: app-only OAuth happens here, so a 401 lands
            # in the except below and the loop moves on.
            client.auth.scopes()
            log.info("using Reddit client id %s", client_id)
            return client
        except Exception as error:  # noqa: BLE001 - try the next identity
            log.warning("Reddit refused client id %s: %s", client_id, error)

    raise ResolveError("Reddit would not accept any available client id")


def _candidate_pairs(options: dict[str, Any]) -> list[tuple[str, str]]:
    """The identities to try, in order: yours first, then the shared public ones (V5's order).

    A configured pair is de-duplicated against the public list, so an operator who sets one of the
    public ids explicitly does not cause it to be attempted twice.
    """
    pairs: list[tuple[str, str]] = []
    configured = options.get("client_id")
    if configured:
        pairs.append((str(configured), str(options.get("client_secret") or "")))

    for public in _FALLBACK_APPS:
        if public not in pairs:
            pairs.append(public)
    return pairs


def _gallery_extension(item: dict[str, Any]) -> str:
    """The extension a gallery entry's own MIME type implies."""
    mime = str(item.get("m") or "")
    if "/" in mime:
        return f".{mime.split('/')[-1]}"
    return ".jpg"


def _attribute(submission: Any, name: str) -> Any:
    """An attribute of a submission, or ``None``.

    PRAW raises on some attributes for some post types, so a plain ``getattr`` with a default is not
    enough — which is why this exists rather than being inlined at five call sites.
    """
    try:
        return getattr(submission, name, None)
    except Exception:  # noqa: BLE001 - a post kind that cannot answer this question
        return None


def resolver(
    *, options: dict[str, Any] | None = None, timeout: float = bridge.DEFAULT_TIMEOUT
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, options=options, timeout=timeout)

    return Resolver(name=ENGINE, attempt=_attempt)
