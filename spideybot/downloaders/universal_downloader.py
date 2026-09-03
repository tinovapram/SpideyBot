import os
from typing import Iterator

import structlog
from spideybot.downloaders.site_downloaders.youtube import YouTubeDownloader
from spideybot.downloaders.site_downloaders.tiktok import TikTokDownloader
from spideybot.downloaders.site_downloaders.pinterest import PinterestDownloader
from spideybot.downloaders.site_downloaders.twitter import TwitterDownloader
from spideybot.downloaders.site_downloaders.spotify import SpotifyDownloader
from spideybot.downloaders.site_downloaders.capcut import CapCutDownloader
from spideybot.downloaders.site_downloaders.linkedin import LinkedInDownloader
from spideybot.downloaders.site_downloaders.snapchat import SnapchatDownloader
from spideybot.downloaders.site_downloaders.soundcloud import SoundCloudDownloader
from spideybot.downloaders.site_downloaders.bluesky import BlueskyDownloader
from spideybot.downloaders.site_downloaders.threads import ThreadsDownloader
from spideybot.downloaders.site_downloaders.tumblr import TumblrDownloader
from spideybot.downloaders.site_downloaders.dailymotion import DailymotionDownloader
from spideybot.downloaders.site_downloaders.douyin import DouyinDownloader
from spideybot.downloaders.site_downloaders.kuaishou import KuaishouDownloader
from spideybot.downloaders.site_downloaders.doodstream import DoodstreamDownloader
from spideybot.downloaders.site_downloaders.streamtape import StreamtapeDownloader
from spideybot.downloaders.site_downloaders.vidara import VidaraDownloader
from spideybot.downloaders.site_downloaders.reddit import RedditDownloader

logger = structlog.get_logger(__name__)

class UniversalDownloader:
    def __init__(self):
        self.downloaders = {
            "youtube": YouTubeDownloader(),
            "tiktok": TikTokDownloader(),
            "pinterest": PinterestDownloader(),
            "twitter": TwitterDownloader(),
            "spotify": SpotifyDownloader(),
            "capcut": CapCutDownloader(),
            "linkedin": LinkedInDownloader(),
            "snapchat": SnapchatDownloader(),
            "soundcloud": SoundCloudDownloader(),
            "bluesky": BlueskyDownloader(),
            "threads": ThreadsDownloader(),
            "tumblr": TumblrDownloader(),
            "dailymotion": DailymotionDownloader(),
            "douyin": DouyinDownloader(),
            "kuaishou": KuaishouDownloader(),
            "doodstream": DoodstreamDownloader(),
            "streamtape": StreamtapeDownloader(),
            "vidara": VidaraDownloader(),
            "reddit": RedditDownloader(),
        }


    def detect_platform(self, url: str) -> str:
        url_lower = url.lower()
        if "youtube.com" in url_lower or "youtu.be" in url_lower:
            return "youtube"
        elif "tiktok.com" in url_lower:
            return "tiktok"
        elif "pinterest.com" in url_lower or "pin.it" in url_lower:
            return "pinterest"
        elif "twitter.com" in url_lower or "x.com" in url_lower:
            return "twitter"
        elif "spotify.com" in url_lower:
            return "spotify"
        elif "capcut.com" in url_lower or "capcut.net" in url_lower:
            return "capcut"
        elif "linkedin.com" in url_lower:
            return "linkedin"
        elif "snapchat.com" in url_lower:
            return "snapchat"
        elif "soundcloud.com" in url_lower:
            return "soundcloud"
        elif "bsky.app" in url_lower:
            return "bluesky"
        elif "threads.net" in url_lower:
            return "threads"
        elif "tumblr.com" in url_lower:
            return "tumblr"
        elif "reddit.com" in url_lower or "redd.it" in url_lower:
            return "reddit"
        elif "dailymotion.com" in url_lower or "dai.ly" in url_lower:
            return "dailymotion"
        elif "douyin.com" in url_lower:
            return "douyin"
        elif "kuaishou.com" in url_lower:
            return "kuaishou"
        elif DoodstreamDownloader.matches(url):
            return "doodstream"
        elif StreamtapeDownloader.matches(url):
            return "streamtape"
        elif VidaraDownloader.matches(url):
            return "vidara"
        return "unknown"

    def download(self, url: str, output_dir: str = "downloads") -> list:
        platform = self.detect_platform(url)
        if platform == "unknown":
            raise ValueError(f"Unsupported platform or URL format: {url}")
        
        downloader = self.downloaders.get(platform)
        if not downloader:
            raise ValueError(f"Downloader for platform '{platform}' not configured")
        
        logger.info("Routing URL to downloader", platform=platform, downloader=downloader.__class__.__name__)
        return downloader.download(url, output_dir=output_dir)

    def download_streaming(self, url: str, output_dir: str = "downloads") -> Iterator[str]:
        """Yield media files one-by-one as they download.

        Delegates to the platform downloader's ``download_streaming()`` when
        available, otherwise falls back to ``download()`` (yielding all at once).
        """
        platform = self.detect_platform(url)
        if platform == "unknown":
            raise ValueError(f"Unsupported platform or URL format: {url}")

        downloader = self.downloaders.get(platform)
        if not downloader:
            raise ValueError(f"Downloader for platform '{platform}' not configured")

        logger.info("Routing URL to streaming downloader", platform=platform)
        if hasattr(downloader, 'download_streaming'):
            yield from downloader.download_streaming(url, output_dir=output_dir)
        else:
            yield from downloader.download(url, output_dir=output_dir)
