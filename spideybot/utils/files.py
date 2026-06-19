"""
SpideyBot — File Utilities.

Filename sanitization, post text/caption extraction from metadata JSON,
and async file download helper.
"""

import os
import re
import json
import asyncio
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)


def sanitize_filename(filename: str, max_len: int = 120) -> str:
    """
    Sanitize filename to comply with both Windows and Linux naming rules,
    and truncate to prevent path length errors.

    Args:
        filename: Original filename string.
        max_len: Maximum length for the name portion (excluding extension).

    Returns:
        A safe, truncated filename string.
    """
    # Split filename and extension
    name, ext = os.path.splitext(filename)

    # Replace invalid chars with underscore
    clean_name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name)
    # Strip leading/trailing spaces and periods
    clean_name = clean_name.strip(" .")

    # Truncate clean_name if too long (reserving space for extension)
    if len(clean_name) > max_len:
        clean_name = clean_name[:max_len].strip(" .")

    if not clean_name:
        clean_name = "file"

    # Re-assemble filename
    clean_ext = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", ext).strip(" .")
    if len(clean_ext) > 10:
        clean_ext = clean_ext[:10]

    return f"{clean_name}.{clean_ext}" if clean_ext else clean_name


def extract_post_text(json_paths: List[str]) -> Optional[str]:
    """
    Extract a post description/caption from gallery-dl metadata JSON files.

    Searches for common text content keys (content, description, caption, etc.)
    across all provided JSON files and returns the best match.

    Args:
        json_paths: List of paths to JSON metadata files.

    Returns:
        The extracted text (truncated to 1024 chars), or None if not found.
    """
    # Target keys to search for, in lowercase
    target_keys = {
        'content', 'description', 'caption', 'title', 'text',
        'desc', 'selftext', 'tweet_text', 'message'
    }

    # Store matches: {key: content}
    found_texts = {}

    def search_recursive(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_lower = k.lower()
                if k_lower == 'category':
                    if isinstance(v, str) and v.strip():
                        if k_lower not in found_texts:
                            found_texts[k_lower] = v.strip()
                elif k_lower == 'author':
                    if isinstance(v, str) and v.strip():
                        if k_lower not in found_texts:
                            found_texts[k_lower] = v.strip()
                    elif isinstance(v,dict):
                        if k_lower not in found_texts:
                            found_texts[k_lower] = v['name'].strip()
                elif k_lower in target_keys:
                    if isinstance(v, str) and v.strip():
                        if k_lower not in found_texts:
                            found_texts[k_lower] = v.strip()
                    elif isinstance(v, dict):
                        # Some structures have nested dicts wrapping the text
                        for sub_k in ['text', 'value', 'content']:
                            sub_v = v.get(sub_k)
                            if sub_v and isinstance(sub_v, str) and sub_v.strip():
                                if k_lower not in found_texts:
                                    found_texts[k_lower] = sub_v.strip()
                                    break
                # Recursively search children
                search_recursive(v)
        elif isinstance(obj, list):
            for item in obj:
                search_recursive(item)

    for jp in json_paths:
        try:
            with open(jp, 'r', encoding='utf-8') as f:
                data = json.load(f)
                search_recursive(data)
        except Exception:
            pass

    # Priority order for selecting the best caption key
    priority_order = [
        'content', 'description', 'caption', 'text', 'desc',
        'selftext', 'tweet_text', 'title', 'message'
    ]
    caption=''
    if found_texts['category']=='reddit':
        caption= found_texts['title'][:1024]
    else:
        for pk in priority_order:
            if pk in found_texts:
                caption= found_texts[pk][:1024]
                break
    if caption:
        caption=f"{found_texts['author']} on {found_texts['category']}:\n\n{caption}\n"
        return caption
    return None


async def download_file_async(tb_downloader, tb_file, output_dir: str) -> str:
    """
    Download a TeraBox file asynchronously using a thread pool executor.

    Args:
        tb_downloader: TeraBoxDownloader instance.
        tb_file: TeraBoxFile with a valid dlink.
        output_dir: Directory to save the downloaded file.

    Returns:
        The path to the downloaded file.
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_filename = sanitize_filename(tb_file.filename)
    filepath = os.path.join(output_dir, safe_filename)

    def _download():
        r = tb_downloader.session.get(
            tb_file.dlink,
            stream=True,
            timeout=tb_downloader.timeout,
            headers={"User-Agent": tb_downloader.USER_AGENT},
        )
        r.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return filepath

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download)
