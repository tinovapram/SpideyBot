"""``/sites`` — list supported platforms (FR-16)."""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

# Platforms are categorized as:
# Social & Video (Twitter, TikTok, etc.)
# Video Hosting (Doodstream, StreamTape, etc.)
# Heavy/Cloud (TeraBox, etc.)

_SITES = """
⚙️ **Supported Sites**

**Social & Video**
• Twitter/X, Reddit, TikTok, Douyin, Threads, Pinterest, Tumblr, 
  Snapchat, LinkedIn, CapCut, Dailymotion, Kuaishou, SoundCloud, Spotify

**Video Hosting**
• Doodstream, StreamTape, MixDrop, StreamWish, Vidmonstr, 
  Vidara, Luluvdoo, Bysejikuar

**Heavy/Cloud**
• TeraBox, MEGA, YouTube

Send links from these sites with `/dl` or `/dt`.
"""

@Client.on_message(filters.command("sites") & filters.private)
async def sites_command(client: Client, message: Message) -> None:
    await message.reply_text(_SITES)
