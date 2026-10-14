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
from downloader.sites import (
    bysejikuar,
    capcut,
    dailymotion,
    doodstream,
    douyin,
    kuaishou,
    linkedin,
    luluvdoo,
    mega,
    mixdrop,
    pinterest,
    reddit,
    snapchat,
    soundcloud,
    spotify,
    streamtape,
    streamwish,
    terabox,
    threads,
    tiktok,
    tumblr,
    twitter,
    vidara,
    vidmonstr,
    youtube,
)

log = logging.getLogger(__name__)

#: The strategies, by the name the user is shown in E2's fallback notice.
GALLERYDL = "gallery-dl"
YTDLP = "yt-dlp"
CYBERDROP = "cyberdrop-dl"

#: Hosts the yt-dlp library resolves directly, and therefore tries first (D5).
VIDEO_HOSTS = probe.YOUTUBE_HOSTS

StopCheck = Callable[[], bool]


def _engine(module: Any, **kwargs: Any) -> Resolver:
    """One row of the native table: a module's own resolver, with the arguments it needs.

    The keyword names differ between adapters on purpose (YouTube needs a preference, Reddit needs
    credentials, the rest need cookies and a timeout), so a row lists only what its adapter takes
    rather than every adapter accepting every argument.
    """
    return module.resolver(**kwargs)


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
            # An extractor that needs a credential gets it as `-o`, from the adapter's own scoped
            # options. Reddit is the one that does: D26's committed config file was never needed,
            # because an option on the command line cannot be shadowed by the operator's own
            # gallery-dl config.
            extractor_options=context.options.get(GALLERYDL),
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


def native_resolvers(
    url: str, context: ChainContext
) -> tuple[Resolver, ...]:
    """Every native adapter that claims this link, in the table's order.

    A table rather than a ladder of ``elif``s. The ladder was one branch per host and worked while
    there were twelve; with twenty-seven it becomes a place where a wrong ``elif`` silently hides
    every host below it, and only a smoke test against the hidden site would notice. A row states
    "this module claims these hosts", the order is the tuple's order, and
    ``tests/unit/test_registry.py`` asserts that no two rows claim the same host and that every
    module in the package appears exactly once.

    Returns *all* matches rather than the first: an adapter that claims a host and then fails is
    exactly the case the next entry in the chain exists for, and a host legitimately served by two
    adapters (a shortener and its target) should try both.
    """
    found: list[Resolver] = []
    for module, arguments, scope in _NATIVE_TABLE:
        if not module.matches(url):
            continue
        resolved = {name: _argument(name, context, scope) for name in arguments}
        found.append(module.resolver(**resolved))
    return tuple(found)


def _argument(name: str, context: ChainContext, scope: str) -> Any:
    """One keyword for an adapter's factory, taken from the context.

    ``options`` is handed over *scoped to the adapter's own name* rather than whole: the caller
    (:func:`app.jobs._adapter_options`) writes ``{"reddit": {...}}``, so this adapter sees the inner
    mapping and never the outer one. That keeps per-site knowledge out of both directions — the
    registry does not know which key an adapter wants, and the caller does not know which argument
    name an adapter's factory happens to use.
    """
    match name:
        case "cookies_file":
            return context.cookies_file
        case "timeout":
            return context.timeout
        case "preference":
            return context.preference
        case "options":
            return dict(context.options.get(scope, {}))
        case _:  # pragma: no cover - a table row naming an argument nothing provides
            raise AssertionError(f"no source for adapter argument {name!r}")


#: ``(module, the keyword arguments its factory takes, its options scope)``. The order is the order
#: they are tried in, and the scope is how an adapter's slice of configuration is addressed.
_NATIVE_TABLE: tuple[tuple[Any, tuple[str, ...], str], ...] = (
    (youtube, ("preference", "timeout", "cookies_file"), "youtube"),
    (terabox, ("cookies_file", "options", "timeout"), "terabox"),
    (doodstream, ("cookies_file", "timeout"), "doodstream"),
    (streamtape, ("cookies_file", "timeout"), "streamtape"),
    (mixdrop, ("cookies_file", "timeout"), "mixdrop"),
    (streamwish, ("cookies_file", "timeout"), "streamwish"),
    (vidmonstr, ("cookies_file", "timeout"), "vidmonstr"),
    (vidara, ("cookies_file", "timeout"), "vidara"),
    (luluvdoo, ("cookies_file", "timeout"), "luluvdoo"),
    (bysejikuar, ("cookies_file", "timeout"), "bysejikuar"),
    (mega, ("cookies_file", "timeout"), "mega"),
    (twitter, ("cookies_file", "timeout"), "twitter"),
    (reddit, ("options", "timeout"), "reddit"),
    (tiktok, ("cookies_file", "timeout"), "tiktok"),
    (douyin, ("cookies_file", "timeout"), "douyin"),
    (threads, ("cookies_file", "timeout"), "threads"),
    (pinterest, ("cookies_file", "timeout"), "pinterest"),
    (tumblr, ("cookies_file", "timeout"), "tumblr"),
    (snapchat, ("cookies_file", "timeout"), "snapchat"),
    (linkedin, ("cookies_file", "timeout"), "linkedin"),
    (capcut, ("cookies_file", "timeout"), "capcut"),
    (dailymotion, ("cookies_file", "timeout"), "dailymotion"),
    (kuaishou, ("cookies_file", "timeout"), "kuaishou"),
    (soundcloud, ("cookies_file", "timeout"), "soundcloud"),
    (spotify, ("cookies_file", "timeout"), "spotify"),
)


def chain_for(url: str, context: ChainContext | None = None) -> tuple[Resolver, ...]:
    """The ordered attempts for one link.

    The delegate is *not* in here: :func:`delegate_for` supplies it, because a strategy that
    downloads cannot be expressed as a resolver (D38).
    """
    settings = context or ChainContext()
    host = probe.host_of(url)

    chain: list[Resolver] = list(settings.natives)
    # A caller-supplied adapter wins over the table for the same host: that is how a test drives one
    # adapter without a network, and how a future per-user override would work.
    claimed = {resolver.name for resolver in chain}
    chain.extend(
        resolver for resolver in native_resolvers(url, settings) if resolver.name not in claimed
    )

    # Engine fallbacks. yt-dlp for the video hosts it resolves natively (D5), then gallery-dl for
    # everything else; the delegate runs after both (D38).
    if is_video_host(host) and YTDLP not in claimed:
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
