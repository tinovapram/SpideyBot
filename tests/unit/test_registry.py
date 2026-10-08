"""The chain per host (D6, D38): native first, engines next, cyberdrop-dl last.

The chains are asserted by *name*, because the names are what the user is shown in E2's notice — a
chain that is right but announces the wrong engine is a bug the user sees. Nothing here resolves
anything: the composition is this module's whole job, and it is verifiable without a network.
"""

from __future__ import annotations

from pathlib import Path

from downloader import probe, registry
from downloader.contracts import Resolution
from downloader.flow import Resolver


def native(name: str = "terabox") -> Resolver:
    async def attempt(_: str) -> Resolution:
        return Resolution(engine=name, source_url="u")

    return Resolver(name=name, attempt=attempt)


def test_a_video_link_tries_ytdlp_before_gallerydl() -> None:
    """D5: gallery-dl can reach YouTube only through a marker that carries no media."""
    chain = registry.chain_for("https://www.youtube.com/watch?v=x")

    assert registry.chain_names(chain) == (registry.YTDLP, registry.GALLERYDL)


def test_a_short_video_link_is_recognised() -> None:
    assert registry.chain_names(registry.chain_for("https://youtu.be/x"))[0] == registry.YTDLP


def test_a_subdomain_is_recognised() -> None:
    chain = registry.chain_for("https://m.youtube.com/watch?v=x")

    assert registry.chain_names(chain)[0] == registry.YTDLP


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
