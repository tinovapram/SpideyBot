"""
Platform detection and downloader registry.

A single registry maps host substrings (or custom matchers) to downloader
instances. Downloaders are instantiated lazily on first ``detect()`` match
so unused site modules stay unloaded at startup.
"""

from __future__ import annotations

from urllib.parse import urlparse

from downloader.base import BaseDownloader


class _LazyEntry:
    """Lightweight entry that instantiates its downloader on first access."""

    __slots__ = ("name", "_factory", "_instance", "matcher")

    def __init__(self, name: str, factory, matcher):
        self.name = name
        self._factory = factory
        self._instance = None
        self.matcher = matcher

    @property
    def downloader(self) -> BaseDownloader:
        if self._instance is None:
            self._instance = self._factory()
        return self._instance


def _host_matcher(substrings: tuple[str, ...]):
    def matches(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(sub in host for sub in substrings)
    return matches


def _lazy_plain(cls, substrings):
    name = cls.__name__.replace("Downloader", "").lower()
    return _LazyEntry(name, cls, _host_matcher(substrings))


def _build_entries():
    from downloader.site.bluesky import BlueskyDownloader
    from downloader.site.capcut import CapCutDownloader
    from downloader.site.dailymotion import DailymotionDownloader
    from downloader.site.doodstream import DoodstreamDownloader
    from downloader.site.douyin import DouyinDownloader
    from downloader.site.kuaishou import KuaishouDownloader
    from downloader.site.linkedin import LinkedInDownloader
    from downloader.site.pinterest import PinterestDownloader
    from downloader.site.reddit import RedditDownloader
    from downloader.site.snapchat import SnapchatDownloader
    from downloader.site.soundcloud import SoundCloudDownloader
    from downloader.site.spotify import SpotifyDownloader
    from downloader.site.mixdrop import MixDropDownloader
    from downloader.site.streamtape import StreamtapeDownloader
    from downloader.site.streamwish import StreamWishDownloader
    from downloader.site.luluvdoo import LuluvdooDownloader
    from downloader.site.bysejikuar import BysejikuarDownloader
    from downloader.site.threads import ThreadsDownloader
    from downloader.site.tiktok import TikTokDownloader
    from downloader.site.tumblr import TumblrDownloader
    from downloader.site.twitter import TwitterDownloader
    from downloader.site.vidara import VidaraDownloader
    from downloader.site.youtube import YouTubeDownloader
    from downloader.site.mega import MegaDownloader
    from downloader.site.cyberdrop import CyberdropDLDownloader

    return [
        _lazy_plain(YouTubeDownloader, ("youtube.com", "youtu.be")),
        _lazy_plain(TikTokDownloader, ("tiktok.com",)),
        _lazy_plain(PinterestDownloader, ("pinterest.com", "pin.it")),
        _lazy_plain(TwitterDownloader, ("twitter.com", "x.com")),
        _lazy_plain(SpotifyDownloader, ("spotify.com",)),
        _lazy_plain(CapCutDownloader, ("capcut.com", "capcut.net")),
        _lazy_plain(LinkedInDownloader, ("linkedin.com",)),
        _lazy_plain(SnapchatDownloader, ("snapchat.com",)),
        _lazy_plain(SoundCloudDownloader, ("soundcloud.com",)),
        _lazy_plain(BlueskyDownloader, ("bsky.app",)),
        _lazy_plain(ThreadsDownloader, ("threads.net",)),
        _lazy_plain(TumblrDownloader, ("tumblr.com",)),
        _lazy_plain(DailymotionDownloader, ("dailymotion.com", "dai.ly")),
        _lazy_plain(DouyinDownloader, ("douyin.com",)),
        _lazy_plain(KuaishouDownloader, ("kuaishou.com",)),
        _LazyEntry("doodstream", DoodstreamDownloader, DoodstreamDownloader.matches),
        _LazyEntry("streamtape", StreamtapeDownloader, StreamtapeDownloader.matches),
        _LazyEntry("vidara", VidaraDownloader, VidaraDownloader.matches),
        _LazyEntry("mixdrop", MixDropDownloader, MixDropDownloader.matches),
        _LazyEntry("streamwish", StreamWishDownloader, StreamWishDownloader.matches),
        _LazyEntry("luluvdoo", LuluvdooDownloader, LuluvdooDownloader.matches),
        _LazyEntry("bysejikuar", BysejikuarDownloader, BysejikuarDownloader.matches),
        _lazy_plain(MegaDownloader, ("mega.nz", "mega.co.nz")),
        _LazyEntry(
            "reddit",
            lambda: _make_reddit(),
            _host_matcher(("reddit.com", "redd.it")),
        ),
        _lazy_plain(CyberdropDLDownloader, (
            "cyberdrop.me", "cyberdrop.to", "cyberdrop.cr",
            "bunkrr.su", "bunkr.",
            "cyberfile.me", "iceyfile.",
            "gofile.io", "saint2faucet.",
        )),
    ]


def _make_reddit():
    from core.config import get_settings
    from downloader.site.reddit import RedditDownloader
    settings = get_settings()
    return RedditDownloader(
        client_id=settings.reddit_praw_client_id or None,
        client_secret=settings.reddit_praw_client_secret or None,
        refresh_token=settings.reddit_praw_refresh_token or None,
    )


class DownloaderRegistry:
    """Lazy singleton mapping URLs to downloaders."""

    def __init__(self) -> None:
        self._entries = _build_entries()

    def detect(self, url: str) -> tuple[str, BaseDownloader] | None:
        for entry in self._entries:
            if entry.matcher(url):
                return entry.name, entry.downloader
        return None


_registry: DownloaderRegistry | None = None


def get_registry() -> DownloaderRegistry:
    global _registry
    if _registry is None:
        _registry = DownloaderRegistry()
    return _registry
