"""Which resolvers to try for a link, in which order (D6, D34, D38).

:mod:`downloader.flow` runs a chain but does not decide one; this module decides. It is the only
place that knows a host, and so the only place that changes when an adapter is added — the pipeline,
the gate, the fetcher and the message catalogue stay untouched.

The order is D6's, with the operator's rule that **cyberdrop-dl is the very end**: the native
adapter for the host if one exists, then the engines that can measure what they resolve (yt-dlp
directly for the video hosts it handles natively, D5; gallery-dl for everything else), then
cyberdrop-dl as a *delegating* last resort — it downloads for us, so it is not a resolver and cannot
sit in the chain (D38). Being last is why it needs no host list: anything the measured paths could
not resolve is exactly what it is there for, and it answers for its own unsupported hosts in its own
output.

Two things are deliberately *not* here:

* **No copy of the tool's host list.** It knows its own hosts; a copy would only go stale.
* **No cookie lookup.** Cookies are per user and per host (D8); this module only passes the file the
  caller already resolved. Where that file comes from is not the registry's business.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from downloader import cyberdropdl, probe
from downloader.contracts import Resolution
from downloader.fetcher import FetchedFile, ProgressHook
from downloader.flow import DelegatingFetch, Resolver
from downloader.selection import Preference

log = logging.getLogger(__name__)

#: The strategies, by the name the user is shown in E2's fallback notice.
GALLERYDL = "gallery-dl"
YTDLP = "yt-dlp"
CYBERDROP = "cyberdrop-dl"

#: Hosts the yt-dlp library resolves directly, and therefore tries first (D5).
VIDEO_HOSTS = probe.YOUTUBE_HOSTS

StopCheck = Callable[[], bool]


@dataclass(frozen=True)
class ChainContext:
    """Everything a resolver may need beyond the URL.

    ``cookies_file`` is one user's cookie file, already resolved by the caller (D8): this module
    never reads a cookie, it only tells the engine where one is.
    """

    preference: Preference = "compatible"
    cookies_file: Path | None = None
    timeout: float = probe.DEFAULT_RESOLVE_TIMEOUT
    #: Native adapters for this link, tried before the engines. Supplied by the caller because which
    #: adapter applies is per-host knowledge that arrives with the adapter itself.
    natives: tuple[Resolver, ...] = ()
    #: Anything an adapter needs from configuration (TeraBox accounts, Reddit credentials…), passed
    #: through untouched so adding an adapter never means changing this dataclass.
    options: Mapping[str, Any] = field(default_factory=dict)


def is_video_host(host: str) -> bool:
    """Whether the yt-dlp library resolves this host directly (D5)."""
    return any(host == marker or host.endswith("." + marker) for marker in VIDEO_HOSTS)


def gallerydl_resolver(context: ChainContext) -> Resolver:
    """gallery-dl, as one entry in a chain."""

    async def attempt(url: str) -> Resolution:
        return await probe.resolve_gallerydl(
            url,
            preference=context.preference,
            timeout=context.timeout,
            cookies_file=context.cookies_file,
        )

    return Resolver(name=GALLERYDL, attempt=attempt)


def ytdlp_resolver(context: ChainContext) -> Resolver:
    """yt-dlp as a library, for the video hosts it handles natively (D5)."""

    async def attempt(url: str) -> Resolution:
        return await probe.resolve_ytdlp(
            url,
            preference=context.preference,
            timeout=context.timeout,
            cookies_file=context.cookies_file,
        )

    return Resolver(name=YTDLP, attempt=attempt)


def chain_for(url: str, context: ChainContext | None = None) -> tuple[Resolver, ...]:
    """The ordered attempts for one link.

    The delegate is *not* in here: :func:`delegate_for` supplies it, because a strategy that
    downloads cannot be expressed as a resolver (D38).
    """
    settings = context or ChainContext()
    host = probe.host_of(url)

    chain: list[Resolver] = list(settings.natives)
    if is_video_host(host):
        chain.append(ytdlp_resolver(settings))
    chain.append(gallerydl_resolver(settings))
    return tuple(chain)


def delegate_for(context: ChainContext | None = None) -> DelegatingFetch | None:
    """The last resort, or ``None`` when the tool is not installed.

    ``None`` rather than an entry that always fails: a chain ending in a known-broken step would
    report its failure to the user as though the link were at fault.
    """
    settings = context or ChainContext()
    if not cyberdropdl.is_available():
        log.warning("cyberdrop-dl is not installed: no last resort will be tried")
        return None

    async def run(
        url: str,
        staging: Path,
        *,
        limit_bytes: int | None,
        progress: ProgressHook | None,
        should_stop: StopCheck | None,
    ) -> tuple[FetchedFile, ...]:
        return await cyberdropdl.download(
            url,
            staging,
            limit_bytes=limit_bytes,
            timeout=settings.timeout,
            progress=progress,
            should_stop=should_stop,
        )

    return DelegatingFetch(name=CYBERDROP, run=run)


def chain_names(chain: Sequence[Resolver]) -> tuple[str, ...]:
    """The chain's strategy names, for a log line or for a test that reads better than indices."""
    return tuple(resolver.name for resolver in chain)
