"""
Platform detection and downloader registry.

A single registry maps host substrings (or custom matchers) to downloader
instances. Built lazily so configured Reddit credentials are used.
"""

from __future__ import annotations

from urllib.parse import urlparse

from core import config
from downloader.base import BaseDownloader
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
from downloader.site.streamtape import StreamtapeDownloader
from downloader.site.threads import ThreadsDownloader
from downloader.site.tiktok import TikTokDownloader
from downloader.site.tumblr import TumblrDownloader
from downloader.site.twitter import TwitterDownloader
from downloader.site.vidara import VidaraDownloader
from downloader.site.youtube import YouTubeDownloader


def _host_matcher(substrings: tuple[str, ...]):
    def matches(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(sub in host for sub in substrings)
    return matches


def _plain(cls, substrings):
    name = cls.__name__.replace("Downloader", "").lower()
    return name, cls(), _host_matcher(substrings)


def _build_entries():
    return [
        _plain(YouTubeDownloader, ("youtube.com", "youtu.be")),
        _plain(TikTokDownloader, ("tiktok.com",)),
        _plain(PinterestDownloader, ("pinterest.com", "pin.it")),
        _plain(TwitterDownloader, ("twitter.com", "x.com")),
        _plain(SpotifyDownloader, ("spotify.com",)),
        _plain(CapCutDownloader, ("capcut.com", "capcut.net")),
        _plain(LinkedInDownloader, ("linkedin.com",)),
        _plain(SnapchatDownloader, ("snapchat.com",)),
        _plain(SoundCloudDownloader, ("soundcloud.com",)),
        _plain(BlueskyDownloader, ("bsky.app",)),
        _plain(ThreadsDownloader, ("threads.net",)),
        _plain(TumblrDownloader, ("tumblr.com",)),
        _plain(DailymotionDownloader, ("dailymotion.com", "dai.ly")),
        _plain(DouyinDownloader, ("douyin.com",)),
        _plain(KuaishouDownloader, ("kuaishou.com",)),
        ("doodstream", DoodstreamDownloader(), DoodstreamDownloader.matches),
        ("streamtape", StreamtapeDownloader(), StreamtapeDownloader.matches),
        ("vidara", VidaraDownloader(), VidaraDownloader.matches),
        (
            "reddit",
            RedditDownloader(
                client_id=config.REDDIT_PRAW_CLIENT_ID or None,
                client_secret=config.REDDIT_PRAW_CLIENT_SECRET or None,
                refresh_token=config.REDDIT_PRAW_REFRESH_TOKEN or None,
            ),
            _host_matcher(("reddit.com", "redd.it")),
        ),
    ]


class DownloaderRegistry:
    """Lazy singleton mapping URLs to downloaders."""

    def __init__(self) -> None:
        self._entries = _build_entries()

    def detect(self, url: str) -> tuple[str, BaseDownloader] | None:
        for name, downloader, matcher in self._entries:
            if matcher(url):
                return name, downloader
        return None


_registry: DownloaderRegistry | None = None


def get_registry() -> DownloaderRegistry:
    global _registry
    if _registry is None:
        _registry = DownloaderRegistry()
    return _registry
