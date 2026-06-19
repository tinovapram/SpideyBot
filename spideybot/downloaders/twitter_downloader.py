import os
import re
import json
import logging
import asyncio
import requests
from bs4 import BeautifulSoup
from typing import List

logger = logging.getLogger(__name__)

def _download_url_to_file(url: str, dest_path: str):
    """Download a URL to a file synchronously (ran in executor)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    r = requests.get(url, stream=True, timeout=30, headers=headers)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

async def _download_files(urls: List[str], dest_dir: str, max_size_bytes: int, progress_callback=None) -> List[str]:
    """Download multiple URLs in parallel and return the local file paths."""
    os.makedirs(dest_dir, exist_ok=True)
    loop = asyncio.get_event_loop()
    
    # 1. Pre-check file sizes if possible
    total_size = 0
    for url in urls:
        try:
            r = requests.head(url, timeout=10, allow_redirects=True)
            total_size += int(r.headers.get("content-length", 0))
        except Exception:
            pass
            
    if total_size > max_size_bytes:
        limit_mb = max_size_bytes / (1024 * 1024)
        raise ValueError(f"Estimated download size exceeds limit of {limit_mb:.1f} MB.")

    # 2. Download files in parallel
    completed_count = 0
    total_files = len(urls)
    
    async def download_one(url: str, dest_path: str) -> str:
        nonlocal completed_count
        await loop.run_in_executor(None, _download_url_to_file, url, dest_path)
        completed_count += 1
        if progress_callback:
            status_text = f"📥 **SpideyBot: Downloading Twitter media...**\n• Files downloaded: {completed_count}/{total_files}"
            await progress_callback(status_text)
        return dest_path

    tasks = []
    for i, url in enumerate(urls):
        # Extract file extension or default to jpg
        ext = ".jpg"
        clean_url = url.split("?")[0]
        match = re.search(r"\.(\w{3,4})$", clean_url)
        if match:
            ext = f".{match.group(1)}"
            if ext.lower() in [".mp4", ".m3u8"]:
                ext = ".mp4"
        
        filename = f"twitter_media_{i}{ext}"
        dest_path = os.path.join(dest_dir, filename)
        tasks.append(download_one(url, dest_path))
        
    # Wait for all downloads to finish in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    downloaded_paths = []
    for res in results:
        if isinstance(res, Exception):
            logger.error(f"Failed to download a Twitter media file: {res}")
        else:
            downloaded_paths.append(res)
            
    return downloaded_paths

async def download_twitter_vxtwitter(url: str, dest_dir: str, max_size_bytes: int, progress_callback=None) -> List[str]:
    """Try to download media using the vxtwitter API."""
    tweet_ids = re.findall(r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})", url)
    if not tweet_ids:
        raise ValueError("Could not extract tweet ID from URL.")
        
    tweet_id = tweet_ids[0]
    api_url = f"https://api.vxtwitter.com/Twitter/status/{tweet_id}"
    
    loop = asyncio.get_event_loop()
    def _fetch_metadata():
        r = requests.get(api_url, timeout=15)
        r.raise_for_status()
        return r.json()
        
    metadata = await loop.run_in_executor(None, _fetch_metadata)
    
    media_urls = []
    for media in metadata.get("media_extended", []):
        media_url = media.get("url")
        if media_url:
            media_urls.append(media_url)
            
    if not media_urls:
        raise ValueError("No media URLs found in vxtwitter response.")
        
    # Download the media files
    local_files = await _download_files(media_urls, dest_dir, max_size_bytes, progress_callback=progress_callback)
    
    # Save a metadata JSON so the existing caption pipeline can pick up the tweet text!
    caption_text = metadata.get("text")
    if caption_text:
        author = metadata.get("user_screen_name") or metadata.get("user_name") or "twitter"
        meta_data = {
            "category": "twitter",
            "author": author,
            "text": caption_text
        }
        meta_path = os.path.join(dest_dir, "metadata.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta_data, f, indent=4)
        local_files.append(meta_path)
        
    return local_files

async def download_twitter_savetwitter(url: str, dest_dir: str, max_size_bytes: int, progress_callback=None) -> List[str]:
    """Try to download media using the savetwitter.net AJAX API."""
    headers = {
        "accept": "*/*",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "content-type": "application/x-www-form-urlencoded"
    }
    data = {
        "q": url,
        "lang": "en"
    }
    
    loop = asyncio.get_event_loop()
    def _fetch_ajax():
        response = requests.post("https://savetwitter.net/api/ajaxSearch", data=data, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
        
    resp_json = await loop.run_in_executor(None, _fetch_ajax)
    response_html = resp_json.get("data")
    if not response_html:
        raise ValueError("No data returned from savetwitter API.")
        
    soup = BeautifulSoup(response_html, 'html.parser')
    media_urls = []
    
    # Check if the content contains videos
    if soup.select("div.tw-video"):
        for content in soup.select("div.tw-video"):
            right_div = content.find('div', class_='tw-right')
            if right_div:
                buttons = right_div.find_all('a', class_='tw-button-dl')
                if buttons:
                    # Choose the best video URL
                    best_video_url = None
                    for button in buttons:
                        label = button.get_text(strip=True).lower()
                        if "download mp4" in label:
                            best_video_url = button.get("href")
                            # If it's a high resolution or default, choose it
                            if "720p" in label or "1080p" in label:
                                best_video_url = button.get("href")
                                break
                    if best_video_url:
                        media_urls.append(best_video_url)
                    else:
                        # Fallback to the first download button if MP4 labels not found
                        media_urls.append(buttons[0].get("href"))
    else:
        # If not a video, assume images
        for image in soup.select("div.video-data > div > ul > li"):
            anchor = image.select_one("div > div:nth-child(2) > a")
            if anchor and anchor.get("href"):
                media_urls.append(anchor["href"])
                
    if not media_urls:
        raise ValueError("No media URLs parsed from savetwitter HTML.")
        
    # Download the media files
    local_files = await _download_files(media_urls, dest_dir, max_size_bytes, progress_callback=progress_callback)
    return local_files

async def download_twitter_fallback(url: str, dest_dir: str, max_size_bytes: int, progress_callback=None) -> List[str]:
    """
    Tries multiple methods to download Twitter/X media without authentication.
    Returns a list of downloaded local file paths (including metadata JSON if available).
    """
    logger.info(f"Triggering Twitter fallback downloader for: {url}")
    
    # Method 1: Try vxtwitter API (fast, JSON, contains caption)
    try:
        logger.info("Attempting Twitter download via vxtwitter API...")
        files = await download_twitter_vxtwitter(url, dest_dir, max_size_bytes, progress_callback=progress_callback)
        if files:
            logger.info(f"Successfully downloaded {len(files)} files via vxtwitter.")
            return files
    except ValueError as ve:
        # Size limit exceeded or invalid URL format - raise immediately
        logger.warning(f"vxtwitter check failed: {ve}")
        raise ve
    except Exception as e:
        logger.warning(f"vxtwitter download failed: {e}. Trying savetwitter.net fallback...")
        
    # Method 2: Try savetwitter.net AJAX scraping
    try:
        logger.info("Attempting Twitter download via savetwitter.net API...")
        files = await download_twitter_savetwitter(url, dest_dir, max_size_bytes, progress_callback=progress_callback)
        if files:
            logger.info(f"Successfully downloaded {len(files)} files via savetwitter.net.")
            return files
    except ValueError as ve:
        # Size limit exceeded or parsing validation failure
        logger.warning(f"savetwitter.net check failed: {ve}")
        raise ve
    except Exception as e:
        logger.error(f"savetwitter.net download failed: {e}")
        raise RuntimeError(f"Twitter download fallback failed: {e}")
