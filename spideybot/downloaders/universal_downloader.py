import os
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
            "kuaishou": KuaishouDownloader()
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
        elif "dailymotion.com" in url_lower or "dai.ly" in url_lower:
            return "dailymotion"
        elif "douyin.com" in url_lower:
            return "douyin"
        elif "kuaishou.com" in url_lower:
            return "kuaishou"
        return "unknown"

    def download(self, url: str, output_dir: str = "downloads") -> list:
        platform = self.detect_platform(url)
        if platform == "unknown":
            raise ValueError(f"Unsupported platform or URL format: {url}")
        
        downloader = self.downloaders.get(platform)
        if not downloader:
            raise ValueError(f"Downloader for platform '{platform}' not configured")
        
        print(f"[INFO] Routing URL to {downloader.__class__.__name__}...")
        return downloader.download(url, output_dir=output_dir)
