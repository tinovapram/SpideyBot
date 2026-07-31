"""
SpideyBot — Progress Callback for Telegram uploads.

Handles rate-limited Telegram message editing with progress bar rendering
for file upload operations.
"""

import time
import asyncio

import structlog
from telethon.errors import RPCError as TelethonRPCError

logger = structlog.get_logger(__name__)


def format_size(bytes_size: float) -> str:
    """Format a byte count into a human-readable string (B/KB/MB/GB/TB)."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


class ProgressCallback:
    """
    Telegram message-editing progress callback for file uploads.

    Renders a progress bar and file size info, rate-limited to one
        edit every 5 seconds to avoid Telegram flood limits.
    Args:
        event: The Telegram event to reply to.
        message: The Telegram message object to edit in-place.
        prefix_text: Optional text to prepend above the progress bar.
        file_index: Current file number (for multi-file uploads).
        total_files: Total number of files being uploaded.
    """

    def __init__(self, event, message, prefix_text: str = "", file_index: int = None, total_files: int = None) -> None:
        self.event = event
        self.message = message
        self.prefix_text = prefix_text
        self.file_index = file_index
        self.total_files = total_files
        self.last_msg_time = time.time()
        self.last_text = ""
        self.last_percent = 0.0

    def update(self, current, total) -> None:
        """
        Called by Telethon during file upload with byte counts.

        Args:
            current: Bytes uploaded so far (or float for chunk-based progress).
            total: Total bytes to upload (or total chunks).
        """
        if not total:
            return

        current_time = time.time()
        # Rate limit updates to every 5 seconds to prevent Telegram flood limits,
        # but always allow the final 100% update and meaningful progress changes.
        if current_time - self.last_msg_time > 5 or current == total:
            if isinstance(current, float):
                # Float/chunk based custom formatting support
                i = int(current) + 1
                percent = (i - 1) * 100.0
                text = f"Upload: {i}/{total} ({percent:.0f}%)"
            else:
                # Bytes-based progress
                percent = (current / total) * 100.0
                completed_blocks = int(percent / 10)
                progress_bar = "■" * completed_blocks + "□" * (10 - completed_blocks)

                size_info = f"({format_size(current)} / {format_size(total)})"

                lines = []
                if self.prefix_text:
                    lines.append(self.prefix_text)
                if self.file_index is not None and self.total_files is not None:
                    lines.append(f"• **Uploading file:** {self.file_index}/{self.total_files}")
                lines.append(f"• **Progress:** `[{progress_bar}]` {percent:.1f}% {size_info}")
                text = "\n".join(lines)

            if text != self.last_text:
                self.last_text = text
                self.last_msg_time = current_time

                async def _do_edit():
                    try:
                        if hasattr(self.message, 'edit'):
                            await self.message.edit(text)
                        elif hasattr(self.message, 'edit_text'):
                            await self.message.edit_text(text)
                    except TelethonRPCError:
                        pass  # Telegram rate-limit or message deleted

                try:
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        loop.create_task(_do_edit())
                except RuntimeError:
                    pass
