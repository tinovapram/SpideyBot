import os
import re
import html
import urllib.parse
import json
import requests
import praw

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

class RedditDownloader:
    def __init__(self, client_id=None, client_secret=None, refresh_token=None, user_agent='my_app/1.0'):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.user_agent = user_agent
        self.reddit = None
        self._authenticate()

    def _authenticate(self):
        # 1. Compile the list of candidate client ID + secret pairs (in order of priority)
        pairs_to_try = []
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
                    print(f"[INFO] Authenticated successfully using Reddit refresh token with client_id: {cid}")
                    return
                except Exception as e:
                    print(f"[Warning] Refresh token auth failed for client_id {cid}: {e}. Trying next candidate.")

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
                    print(f"[INFO] Authenticated successfully using manual access token with client_id: {cid}")
                    return
            except Exception as e:
                print(f"[Warning] Manual token auth failed for client_id {cid}: {e}. Trying standard PRAW auth.")

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
                print(f"[INFO] Authenticated using standard PRAW client credentials with client_id: {cid}")
                return
            except Exception as e:
                print(f"[Warning] Standard PRAW auth failed for client_id {cid}: {e}.")

        raise RuntimeError("Failed to authenticate with Reddit API using any of the available credential pairs.")

    def sanitize_filename(self, filename: str) -> str:
        # Replace invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = filename.replace(' ', '_')
        filename = filename.strip('. ')
        return filename[:200]

    def download(self, url: str, output_dir: str = "downloads") -> list:
        """
        Download media from a Reddit submission URL.
        Returns a list of downloaded file paths.
        """
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        print(f"Resolving Reddit submission: {url}")
        submission = self.reddit.submission(url=url)
        
        # Access attributes to force lazy loading
        title = submission.title
        upvotes = submission.ups
        print(f"Post Title: {title} (Upvotes: {upvotes})")

        safe_title = self.sanitize_filename(title)
        downloaded_paths = []

        # 1. Check if it's a video
        if getattr(submission, 'is_video', False):
            print("Detected video post.")
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
                    print("Attempting to download with yt-dlp for merged audio/video...")
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
                            print(f"Success! Video downloaded via yt-dlp: {path}")
                            break
                except Exception as e:
                    print(f"[Warning] yt-dlp download failed: {e}. Falling back to direct video URL download.")
            
            if not ytdlp_success:
                print(f"Downloading video from fallback url: {video_url}")
                resp = requests.get(video_url, stream=True)
                resp.raise_for_status()
                with open(file_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                downloaded_paths.append(file_path)
                print(f"Success! Direct video downloaded (no audio): {file_path}")

        # 2. Check if it's a gallery
        elif getattr(submission, 'is_gallery', False):
            print("Detected gallery post.")
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
                            print(f"Downloading gallery image {index}: {img_url}")
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
            print(f"Detected standard link/image post. URL: {post_url}")
            if post_url.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                ext = os.path.splitext(urllib.parse.urlparse(post_url).path)[1] or '.jpg'
                file_path = os.path.join(output_dir, f"{upvotes}_{safe_title}{ext}")
                print(f"Downloading image: {post_url}")
                resp = requests.get(post_url, stream=True)
                resp.raise_for_status()
                with open(file_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                downloaded_paths.append(file_path)
                print(f"Success! Image downloaded: {file_path}")
            else:
                # If it's not a direct image URL, try downloading via yt-dlp
                ytdlp_success = False
                if HAS_YTDLP:
                    try:
                        print("Attempting to download with yt-dlp...")
                        ydl_opts = {
                            'outtmpl': os.path.join(output_dir, f"{upvotes}_{safe_title}.%(ext)s"),
                            'quiet': True,
                            'no_warnings': True,
                        }
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([post_url])
                        
                        import glob
                        pattern = os.path.join(output_dir, f"{upvotes}_{safe_title}.*")
                        files = glob.glob(pattern)
                        if files:
                            downloaded_paths.extend(files)
                            ytdlp_success = True
                            print(f"Success! Downloaded via yt-dlp: {files}")
                    except Exception as e:
                        print(f"[Warning] yt-dlp download failed: {e}")
                
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
            print(f"Saved metadata JSON: {meta_path}")
        except Exception as e:
            print(f"[Warning] Failed to save metadata JSON: {e}")

        return downloaded_paths
