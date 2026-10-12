"""The chain per host (D6, D38): native first, engines next, cyberdrop-dl last.

The chains are asserted by *name*, because the names are what the user is shown in E2's notice — a
chain that is right but announces the wrong engine is a bug the user sees. Nothing here resolves
anything: the composition is this module's whole job, and it is verifiable without a network.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

import pytest

from downloader import probe, registry
from downloader.contracts import Resolution
from downloader.flow import Resolver


def native(name: str = "terabox") -> Resolver:
    async def attempt(_: str) -> Resolution:
        return Resolution(engine=name, source_url="u")

    return Resolver(name=name, attempt=attempt)


def test_a_video_link_tries_ytdlp_before_gallerydl() -> None:
    """D5: gallery-dl can reach YouTube only through a marker that carries no media.
    
    Now with native youtube resolver first (D6), then yt-dlp library, then gallery-dl.
    """
    chain = registry.chain_for("https://www.youtube.com/watch?v=x")

    assert registry.chain_names(chain) == ("youtube", registry.YTDLP, registry.GALLERYDL)


def test_a_short_video_link_is_recognised() -> None:
    assert registry.chain_names(registry.chain_for("https://youtu.be/x"))[0] == "youtube"


def test_a_subdomain_is_recognised() -> None:
    chain = registry.chain_for("https://m.youtube.com/watch?v=x")

    assert registry.chain_names(chain)[0] == "youtube"


def test_a_lookalike_host_is_not_a_video_host() -> None:
    """``notyoutube.com`` must not be handed to yt-dlp, or the chain announces the wrong engine."""
    chain = registry.chain_for("https://notyoutube.com/x")

    assert registry.chain_names(chain) == (registry.GALLERYDL,)


def test_an_ordinary_link_goes_to_gallery_dl_only() -> None:
    chain = registry.chain_for("https://imgur.com/gallery/abc")

    assert registry.chain_names(chain) == (registry.GALLERYDL,)


def test_a_native_adapter_is_tried_before_the_engines() -> None:
    """A host with an adapter never pays for an engine process first."""
    chain = registry.chain_for(
        "https://www.terabox.com/s/abc",
        registry.ChainContext(natives=(native(),)),
    )

    assert registry.chain_names(chain) == ("terabox", registry.GALLERYDL)


def test_a_native_adapter_keeps_ytdlp_after_it_for_video_hosts() -> None:
    chain = registry.chain_for(
        "https://www.youtube.com/watch?v=x",
        registry.ChainContext(natives=(native("youtube"),)),
    )

    assert registry.chain_names(chain) == ("youtube", registry.YTDLP, registry.GALLERYDL)


def test_the_delegate_is_cyberdrop_dl_and_is_not_a_resolver() -> None:
    """D38: it downloads rather than resolving, so it cannot be an entry in the chain."""
    delegate = registry.delegate_for()

    assert delegate is not None
    assert delegate.name == registry.CYBERDROP
    assert registry.CYBERDROP not in registry.chain_names(registry.chain_for("https://x.test/a"))


def test_the_delegate_is_absent_when_the_tool_is_missing(monkeypatch) -> None:
    """Absent rather than present-and-broken: a failing last step would blame the link."""
    monkeypatch.setattr(registry.cyberdropdl, "is_available", lambda: False)

    assert registry.delegate_for() is None


def test_a_cookie_file_reaches_the_engine() -> None:
    """FR-14/D8: one user's cookie file, per request, and only when there is one."""
    with_cookies = probe.gallerydl_command("https://x.test/a", cookies_file=Path("c/jar.txt"))
    without = probe.gallerydl_command("https://x.test/a")

    assert "--cookies" in with_cookies
    assert with_cookies[with_cookies.index("--cookies") + 1] == str(Path("c/jar.txt"))
    # The URL stays last, so nothing can be mistaken for it.
    assert with_cookies[-1] == "https://x.test/a"
    assert "--cookies" not in without


def test_an_extractor_credential_reaches_the_engine_as_an_option() -> None:
    """Reddit needs a client id, and it travels as ``-o`` rather than in a config file.

    A committed config file was the original plan (D26); an option is better because it cannot be
    shadowed by whatever the operator has in their own gallery-dl configuration, which is the same
    reason the ytdl flag is passed this way.
    """
    command = probe.gallerydl_command(
        "https://www.reddit.com/r/x/comments/1/y",
        extractor_options={"extractor.reddit.client-id": "abc"},
    )

    assert "-o" in command
    assert "extractor.reddit.client-id=abc" in command
    assert command[-1] == "https://www.reddit.com/r/x/comments/1/y"


def test_the_context_carries_the_preference_and_the_cookiefile_into_the_resolver() -> None:
    """The chain is built from the context, so a per-user setting cannot be forgotten later."""
    context = registry.ChainContext(preference="quality", cookies_file=Path("u/1.txt"))
    chain = registry.chain_for("https://imgur.com/gallery/abc", context)

    assert registry.chain_names(chain) == (registry.GALLERYDL,)
    assert chain[0].name == registry.GALLERYDL


def test_host_of_strips_www_so_a_rule_does_not_name_both() -> None:
    assert probe.host_of("https://WWW.YouTube.com/watch?v=x") == "youtube.com"
    assert probe.host_of("https://youtube.com/watch?v=x") == "youtube.com"


def test_is_video_host_matches_subdomains_but_not_lookalikes() -> None:
    assert registry.is_video_host("youtube.com") is True
    assert registry.is_video_host("m.youtube.com") is True
    assert registry.is_video_host("notyoutube.com") is False
    assert registry.is_video_host("youtube.com.evil.test") is False


# ── the native table, and the host knowledge in it ───────────────────────────


#: One URL per adapter, so a new adapter's *absence* from the chain is a test failure rather than a
#: discovery made by a user whose link quietly fell through to gallery-dl.
HOST_FOR_ADAPTER: dict[str, str] = {
    "youtube": "https://www.youtube.com/watch?v=x",
    "terabox": "https://www.terabox.com/s/abc",
    "doodstream": "https://dood.stream/d/abc",
    "streamtape": "https://streamtape.com/v/abc",
    "mixdrop": "https://mixdrop.co/e/abc",
    "streamwish": "https://streamwish.to/e/abc",
    "vidmonstr": "https://vidmonstr.com/v/abc",
    "vidara": "https://vidara.com/v/abc",
    "luluvdoo": "https://luluvdoo.com/v/abc",
    "bysejikuar": "https://bysejikuar.com/v/abc",
    "mega": "https://mega.nz/file/abc",
    "twitter": "https://twitter.com/user/status/1",
    "reddit": "https://www.reddit.com/r/x/comments/1/y",
    "tiktok": "https://www.tiktok.com/@user/video/1",
    "douyin": "https://www.douyin.com/video/1",
    "threads": "https://www.threads.net/@user/post/1",

    "pinterest": "https://www.pinterest.com/pin/1/",
    "tumblr": "https://staff.tumblr.com/post/1",
    "snapchat": "https://www.snapchat.com/spotlight/1",
    "linkedin": "https://www.linkedin.com/posts/1",
    "capcut": "https://www.capcut.com/watch/1",
    "dailymotion": "https://www.dailymotion.com/video/1",
    "kuaishou": "https://www.kuaishou.com/short-video/1",
    "soundcloud": "https://soundcloud.com/user/track",
    "spotify": "https://open.spotify.com/track/1",
}


@pytest.mark.parametrize(("name", "link"), sorted(HOST_FOR_ADAPTER.items()))
def test_every_adapter_is_reachable_by_its_own_host(name: str, link: str) -> None:
    """Each native adapter claims at least the host it was written for.

    The failing mode this prevents is silent: a host no row claims falls through to gallery-dl, so
    the user gets nothing (or the wrong thing) rather than an error naming the adapter that should
    have handled it.
    """
    chain = registry.chain_names(registry.chain_for(link))

    assert name in chain, f"{name} is not in the chain for {link}: {chain}"
    assert chain[0] == name, "the native adapter runs before either engine"


def test_no_two_adapters_claim_the_same_host() -> None:
    """Two rows claiming one host is how a chain ends up trying the wrong adapter first."""
    claims: dict[str, list[str]] = {}
    for name, link in HOST_FOR_ADAPTER.items():
        host = probe.host_of(link)
        claims.setdefault(host, []).append(name)

    duplicated = {host: names for host, names in claims.items() if len(names) > 1}
    assert duplicated == {}, f"more than one adapter names these hosts: {duplicated}"


def test_every_module_in_the_sites_package_is_wired_into_the_table() -> None:
    """A module that exists but is not in the table is dead code pretending to be an adapter."""
    package = importlib.import_module("downloader.sites")
    modules = {
        name
        for _, name, _ in pkgutil.iter_modules(package.__path__)
        # `bridge` is shared plumbing and `__init__` is the package itself: neither is an adapter.
        if name not in {"bridge", "__init__"}
    }
    wired = {module.__name__.rsplit(".", 1)[-1] for module, _, _ in registry._NATIVE_TABLE}

    assert modules - wired == set(), (
        "these site modules are never tried: they are missing from registry._NATIVE_TABLE"
    )
    assert wired - modules == set(), "the table names modules that do not exist"


def test_an_options_scope_is_reachable_from_the_context() -> None:
    """D12's seam: an adapter's credentials arrive under its own name, and the registry never reads
    them. Asserted on Reddit because it is the only adapter that needs one, so a regression here is
    a silent 'Reddit refused every client id' at runtime rather than a test failure."""
    captured: dict[str, Any] = {}

    class Spy:
        name = "reddit"

        @staticmethod
        def matches(url: str) -> bool:
            return True

        @staticmethod
        def resolver(*, options: dict[str, Any], timeout: float) -> Resolver:
            captured.update(options)

            async def attempt(_: str) -> Resolution:
                return Resolution(engine="reddit", source_url="u")

            return Resolver(name="reddit", attempt=attempt)

    import downloader.registry as module

    original = module._NATIVE_TABLE
    module._NATIVE_TABLE = ((Spy, ("options", "timeout"), "reddit"),)
    try:
        context = registry.ChainContext(options={"reddit": {"client_id": "abc"}})
        registry.native_resolvers("https://www.reddit.com/r/x/comments/1/y", context)
    finally:
        module._NATIVE_TABLE = original

    assert captured == {"client_id": "abc"}, (
        "the adapter should see its own slice, not the whole options mapping"
    )


def test_the_table_is_the_whole_chain_for_a_native_host() -> None:
    """A native host still gets the engines behind it, so an adapter's failure is not the end."""
    chain = registry.chain_names(registry.chain_for("https://streamtape.com/v/abc"))

    assert chain == ("streamtape", registry.GALLERYDL)


# ── the contract every wired adapter must honour ─────────────────────────────


class DeadTransport:
    """A session whose every call fails, so an adapter cannot reach anything.

    Used to prove each adapter *fails cleanly* rather than what it resolves: the chain catches
    ``ResolveError`` and moves on, so anything else — a ``TypeError``, an ``AttributeError``, a
    dataclass raised as an exception — escapes the chain and fails the whole job. It also counts its
    calls, because an adapter that ignored the injected session and built its own would be reaching
    the real network behind the test's back.
    """

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.cookies = self
        self.calls: list[str] = []

    def set(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("get")
        raise OSError("the transport is down")

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("post")
        raise OSError("the transport is down")

    async def head(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("head")
        raise OSError("the transport is down")

    async def close(self) -> None:
        return None


# Adapters that refuse before touching any transport, and why. Listed rather than inferred, because
# the reason is per site: TeraBox reads the cookie *values* (`ndus`/`ndut`) and gives up when they
# are absent, so with no cookie file there is nothing for a transport to do.
_REFUSES_BEFORE_ANY_REQUEST = {"terabox": "needs a logged-in cookie file (ndus/ndut)"}


@pytest.mark.parametrize(
    ("module", "arguments", "scope"),
    registry._NATIVE_TABLE,
    ids=lambda row: getattr(row, "__name__", ""),
)
async def test_every_wired_adapter_fails_cleanly(
    module: Any, arguments: tuple[str, ...], scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each adapter, unable to reach its site, must raise ``ResolveError`` and nothing else.

    This is the guard for a real crash: the first port of the Cyberdrop adapter raised a
    ``DelegatingFetch`` dataclass as though it were an exception, so every Cyberdrop link produced a
    ``TypeError`` that the chain could not catch — the job failed instead of falling through to the
    engines. Nothing caught it because the registry's tests only ever inspected chain *names*, and
    no test had ever called an adapter.

    ``ResolveError`` is also accepted for a different reason per host — "no cookies", "the bridge is
    down", "empty post" — all of which are the same contract from the chain's point of view.
    """
    from downloader.contracts import ResolveError

    name = module.__name__.rsplit(".", 1)[-1]

    # Only the arguments the adapter actually declares, and only the values the registry itself
    # passes, so a signature change breaks this test rather than being papered over by a default.
    kwargs: dict[str, Any] = {}
    for argument in arguments:
        if argument == "timeout":
            kwargs["timeout"] = 30.0
        elif argument == "options":
            kwargs["options"] = {}
        # `cookies_file` is left out on purpose: without one, an adapter that needs a login fails
        # before it builds a request, which is the cheapest way to prove it reports that cleanly.

    # The two adapters that own their transport cannot be reached through an injected session, so
    # the transport they do use is broken instead.
    transport: DeadTransport | None = None
    if name == "youtube":

        async def dead_resolve(*args: Any, **kwargs: Any) -> Any:
            raise ResolveError("the video resolver is unavailable")

        monkeypatch.setattr(registry.probe, "resolve_ytdlp", dead_resolve)
    elif name == "reddit":

        def dead_client(*args: Any, **kwargs: Any) -> Any:
            raise ResolveError("Reddit would not accept any available client id")

        monkeypatch.setattr(module, "_client", dead_client)
    else:
        transport = DeadTransport()
        kwargs["client"] = transport

    with pytest.raises(ResolveError):
        await module.attempt("https://example.test/whatever", **kwargs)

    if transport is not None and name not in _REFUSES_BEFORE_ANY_REQUEST:
        assert transport.calls, (
            f"{name} never used the session it was handed, so it is either ignoring the switch's "
            f"client or reaching the network on its own"
        )
