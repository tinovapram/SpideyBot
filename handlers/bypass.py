"""``/bypass`` – shortener resolution command.

The command simply forwards the first argument to the shared resolver.
It is deliberately minimal.  The real implementation would perform
quoting, rate‑limiting, and integration with the normal downloader
pipeline.
"""

from __future__ import annotations

import asyncio

from pyrogram import Client, filters
from pyrogram.types import Message

from downloader.bypass import resolve


@Client.on_message(filters.command("bypass") & filters.private)
async def bypass_command(client: Client, message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.reply_text("Usage: /bypass <shortener URL>")
        return
    try:
        final_url = await asyncio.to_thread(resolve, parts[1])
    except ValueError as exc:
        await message.reply_text(f"❌ {exc}")
    except Exception:
        await message.reply_text("❌ Bypass failed or timed out; please try again later.")
    else:
        await message.reply_text(f"✅ Resolved: {final_url}")
