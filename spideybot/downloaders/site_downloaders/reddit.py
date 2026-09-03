import os
import re
import html
import glob
import urllib.parse
import json
import requests
import praw
from typing import Iterator

import structlog

from spideybot.utils.files import sanitize_filename

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

logger = structlog.get_logger(__name__)

class RedditDownloader:
    def __init__(self, client_id=None, client_secret=None, refresh_token=None, refresh_token_client_id=None, refresh_token_client_secret=None, user_agent='SpideyBot/1.0 (+https://github.com/SpideyBot)'):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.refresh_token_client_id = refresh_token_client_id
        self.refresh_token_client_secret = refresh_token_client_secret
        self.user_agent = user_agent
        self.reddit = None
        self._authenticate()

    def _authenticate(self):
        # 1. Compile the list of candidate client ID + secret pairs (in order of priority)
        pairs_to_try = []
        # If the refresh token was created for a different app, try that pair FIRST
        if self.refresh_token and self.refresh_token_client_id:
            pairs_to_try.append((self.refresh_token_client_id, self.refresh_token_client_secret or ''))
        if self.client_id:
            if self.client_secret:
                pairs_to_try.append((self.client_id, self.client_secret))
            else:
                pairs_to_try.append((self.client_id, ""))
                # Try fallback secrets in case PRAW/Reddit requires a non-empty secret
                pairs_to_try.append((self.client_id, "pzdmKpdnB_JBO0eVkL1IZoxu3vdpug"))
                pairs_to_try.append((self.client_id, "yD_McmeM_yocK0IZYaBeUg8RSBsuqQ"))
                pairs_to_try.append((self.client_id, "gRE6tl_S5ahYX2I5IF3Od29yO7fusA"))
            
        # Redundant client ID + secret pairs
        default_pairs = [
            ('icuiw6HNK21QoQ', 'pzdmKpdnB_JBO0eVkL1IZoxu3vdpug'),
            ('b70jdVnmRpFmVc12GvVuAw', 'yD_McmeM_yocK0IZYaBeUg8RSBsuqQ'),
            ('pdLe8qrK8y3KUpQqFMH63A', 'gRE6tl_S5ahYX2I5IF3Od29yO7fusA'),
        ]
        
        for pair in default_pairs:
            if pair not in pairs_to_try:
                pairs_to_try.append(pair)

        # 2. Try refresh token authentication first if available
        if self.refresh_token:
            for cid, csec in pairs_to_try:
                try:
                    self.reddit = praw.Reddit(
                        client_id=cid,
                        client_secret=csec,
                        refresh_token=self.refresh_token,
                        user_agent=self.user_agent
                    )
                    # Actively validate connection by fetching scopes (throws on bad credentials)
                    self.reddit.auth.scopes()
                    self.client_id = cid
                    self.client_secret = csec
                    logger.info("Authenticated via refresh token", client_id=cid)
                    return
                except Exception as e:
                    logger.warning("Refresh token auth failed, trying next candidate", client_id=cid, error=str(e))

        # 3. Try client credentials flow (manual token first, then standard PRAW client credentials)
        for cid, csec in pairs_to_try:
            # A. Try manual token authentication
            try:
                auth = requests.auth.HTTPBasicAuth(cid, csec)
                headers = {'User-Agent': self.user_agent}
                data = {'grant_type': 'client_credentials'}
                response = requests.post('https://www.reddit.com/api/v1/access_token', auth=auth, data=data, headers=headers)
                response.raise_for_status()
                access_token = response.json().get('access_token')
                
                if access_token:
                    self.reddit = praw.Reddit(
                        client_id=cid,
                        client_secret=csec,
                        user_agent=self.user_agent,
                        access_token=access_token
                    )
                    # Actively validate connection by fetching scopes (throws on bad credentials)
                    self.reddit.auth.scopes()
                    self.client_id = cid
                    self.client_secret = csec
                    logger.info("Authenticated via manual access token", client_id=cid)
                    return
            except Exception as e:
                logger.warning("Manual token auth failed, trying standard PRAW auth", client_id=cid, error=str(e))

            # B. Try standard PRAW authentication
            try:
                self.reddit = praw.Reddit(
                    client_id=cid,
                    client_secret=csec,
                    user_agent=self.user_agent
                )
                # Actively validate connection by fetching scopes (throws on bad credentials)
                self.reddit.auth.scopes()
                self.client_id = cid
                self.client_secret = csec
                logger.info("Authenticated via standard PRAW client credentials", client_id=cid)
                return
            except Exception as e:
                logger.warning("Standard PRAW auth failed", client_id=cid, error=str(e))

        raise RuntimeError("Failed to authenticate with Reddit API using any of the available credential pairs.")

    def download(self, url: str, output_dir: str = "downloads") -> list:
        """
        Download media from a Reddit submission URL.
        Returns a list of downloaded file paths.
        """
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        logger.info("Resolving Reddit submission", url=url)
        submission = self.reddit.submission(url=url)
        
        # Access attributes to force lazy loading
        title = submission.title
        upvotes = submission.ups
        logger.info("Post resolved", title=title, upvotes=upvotes)

        safe_title = sanitize_filename(title)
        downloaded_paths = []

        # 1. Check if it's a video
        if getattr(submission, 'is_video', False):
            logger.info("Detected video post.")
            video_url = None
            if submission.media and 'reddit_video' in submission.media:
                video_url = submission.media['reddit_video'].get('fallback_url')
            
            if not video_url:
                raise ValueError("Could not find video fallback URL in submission media.")

            file_path = os.path.join(output_dir, f"{upvotes}_{safe_title}.mp4")
            
            # Try downloading with yt-dlp first to merge video + audio
            ytdlp_success = False
            if HAS_YTDLP:
                try:
                    logger.info("Attempting to download with yt-dlp for merged audio/video...")
                    # Pass the original post url to yt-dlp so it can find and merge the audio track
                    ydl_opts = {
                        'outtmpl': os.path.join(output_dir, f"{upvotes}_{safe_title}.%(ext)s"),
                        'format': 'bestvideo+bestaudio/best',
                        'merge_output_format': 'mp4',
                        'quiet': True,
                        'no_warnings': True,
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                    
                    expected_files = [
                        os.path.join(output_dir, f"{upvotes}_{safe_title}.mp4"),
                        os.path.join(output_dir, f"{upvotes}_{safe_title}.mkv")
                    ]
                    for path in expected_files:
                        if os.path.exists(path):
                            downloaded_paths.append(path)
                            ytdlp_success = True
                            break
                    logger.info("Downloaded video via yt-dlp", path=path)
                except Exception as e:
                    logger.warning("yt-dlp download failed, falling back to direct video URL", error=str(e))
            
            if not ytdlp_success:
                logger.info("Downloading video from fallback URL", url=video_url)
                resp = requests.get(video_url, stream=True)
                resp.raise_for_status()
                with open(file_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                downloaded_paths.append(file_path)
                logger.info("Direct video downloaded (no audio)", path=file_path)

        # 2. Check if it's a gallery
        elif getattr(submission, 'is_gallery', False):
            logger.info("Detected gallery post.")
            if hasattr(submission, 'media_metadata'):
                index = 1
                for item_id, item in submission.media_metadata.items():
                    if item.get('status') == 'valid' and 's' in item:
                        img_url = item['s'].get('u')
                        if img_url:
                            img_url = html.unescape(img_url)
                            # Determine extension
                            ext = '.jpg'
                            if 'm' in item:
                                mime = item['m']
                                if '/' in mime:
                                    ext = f".{mime.split('/')[-1]}"
                            
                            file_path = os.path.join(output_dir, f"{upvotes}_{safe_title}_{index}{ext}")
                            logger.info("Downloading gallery image", index=index, url=img_url)
                            resp = requests.get(img_url, stream=True)
                            resp.raise_for_status()
                            with open(file_path, 'wb') as f:
                                for chunk in resp.iter_content(chunk_size=8192):
                                    f.write(chunk)
                            downloaded_paths.append(file_path)
                            index += 1
            else:
                raise ValueError("Gallery post has no media_metadata.")

        # 3. Check if it's a simple image
        else:
            post_url = submission.url
            logger.info("Detected standard link/image post", url=post_url)
            if post_url.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                ext = os.path.splitext(urllib.parse.urlparse(post_url).path)[1] or '.jpg'
                file_path = os.path.join(output_dir, f"{upvotes}_{safe_title}{ext}")
                logger.info("Downloading image", url=post_url)
                resp = requests.get(post_url, stream=True)
                resp.raise_for_status()
                with open(file_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                downloaded_paths.append(file_path)
                logger.info("Image downloaded", path=file_path)
            else:
                # If it's not a direct image URL, try downloading via yt-dlp
                ytdlp_success = False
                if HAS_YTDLP:
                    try:
                        logger.info("Attempting to download with yt-dlp...")
                        ydl_opts = {
                            'outtmpl': os.path.join(output_dir, f"{upvotes}_{safe_title}.%(ext)s"),
                            'quiet': True,
                            'no_warnings': True,
                        }
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([post_url])
                        
                        pattern = os.path.join(output_dir, f"{upvotes}_{safe_title}.*")
                        files = glob.glob(pattern)
                        if files:
                            downloaded_paths.extend(files)
                            ytdlp_success = True
                            logger.info("Downloaded via yt-dlp", files=files)
                    except Exception as e:
                        logger.warning("yt-dlp download failed", error=str(e))
                
                if not ytdlp_success:
                    raise ValueError(f"Submission URL is not a supported media format and yt-dlp failed: {post_url}")

        # Save metadata JSON so the existing caption pipeline can extract proper post info
        try:
            author_name = submission.author.name if submission.author else "reddit"
            meta_data = {
                "category": "reddit",
                "author": author_name,
                "title": title,
                "selftext": submission.selftext if getattr(submission, 'selftext', None) else ""
            }
            meta_path = os.path.join(output_dir, "metadata.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_data, f, indent=4)
            downloaded_paths.append(meta_path)
            logger.info("Saved metadata JSON", path=meta_path)
        except Exception as e:
            logger.warning("Failed to save metadata JSON", error=str(e))

        return downloaded_paths

    def download_streaming(self, url: str, output_dir: str = "downloads") -> Iterator[str]:
        """Yield media files one-by-one as they download.

        For gallery posts this yields each image immediately after downloading.
        For video/single-image posts the behaviour is identical to ``download()``.
        """
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        submission = self.reddit.submission(url=url)
        title = submission.title
        upvotes = submission.ups
        safe_title = sanitize_filename(title)

        if getattr(submission, 'is_video', False):
            # Video — single file, delegate to sync download
            yield from self.download(url, output_dir)
            return

        if getattr(submission, 'is_gallery', False) and hasattr(submission, 'media_metadata'):
            for item_id, item in submission.media_metadata.items():
                if item.get('status') == 'valid' and 's' in item:
                    img_url = item['s'].get('u')
                    if not img_url:
                        continue
                    img_url = html.unescape(img_url)
                    ext = '.jpg'
                    if 'm' in item:
                        mime = item['m']
                        if '/' in mime:
                            ext = f".{mime.split('/')[-1]}"
                    file_path = os.path.join(output_dir, f"{upvotes}_{safe_title}_{item_id}{ext}")
                    resp = requests.get(img_url, stream=True)
                    resp.raise_for_status()
                    with open(file_path, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            f.write(chunk)
                    yield file_path
        else:
            # Single image or yt-dlp — delegate to sync download
            yield from self.download(url, output_dir)
            return

        # Metadata — yield last
        try:
            author_name = submission.author.name if submission.author else "reddit"
            meta_data = {
                "category": "reddit",
                "author": author_name,
                "title": title,
                "selftext": submission.selftext if getattr(submission, 'selftext', None) else ""
            }
            meta_path = os.path.join(output_dir, "metadata.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_data, f, indent=4)
            yield meta_path
        except Exception as e:
            logger.warning("Failed to save metadata JSON", error=str(e))
