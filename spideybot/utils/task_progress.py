"""
SpideyBot — Unified Download + Upload Progress Tracker.

Provides a single `TaskProgress` instance per download task that tracks
per-file status (PENDING → DOWNLOADING → UPLOADING → SENT / FAILED) and
renders a Telegram-friendly progress message with bar, speed, and ETA.

Works with both patterns:
  - Sequential (download_handler): download all → upload all
  - Pipeline (terabox_handler): download next while uploading current
"""

import time
from enum import Enum

import structlog
from telethon.errors import RPCError as TelethonRPCError
from dataclasses import dataclass, field
from typing import List, Optional

logger = structlog.get_logger(__name__)


class FileStatus(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class FileProgress:
    """Tracks progress for a single file."""
    index: int
    filename: str
    status: FileStatus = FileStatus.PENDING
    total_bytes: int = 0
    current_bytes: int = 0
    error: Optional[str] = None
    start_time: float = 0.0

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, (self.current_bytes / self.total_bytes) * 100)


def _fmt_size(b: float) -> str:
    """Format bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _fmt_speed(bps: float) -> str:
    """Format bytes/sec to human-readable speed."""
    if bps <= 0:
        return "---"
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / (1024 * 1024):.1f} MB/s"


def _fmt_eta(seconds: float) -> str:
    """Format seconds to human-readable ETA."""
    if seconds <= 0 or seconds > 86400:
        return "---"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(seconds), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m"


def _progress_bar(percent: float, width: int = 10) -> str:
    """Render a Unicode progress bar."""
    filled = int(percent / 100 * width)
    return "█" * filled + "░" * (width - filled)


class TaskProgress:
    """
    Unified progress tracker for a download task.

    Usage:
        tp = TaskProgress("My Share", total_files=5)
        tp.add_file(0, "video.mp4")
        tp.add_file(1, "image.jpg")
        tp.mark_downloading(0, total_bytes=50_000_000)
        tp.update_download(0, current_bytes=25_000_000)
        msg = await tp.update_message(status_msg)
    """

    # Minimum interval between Telegram message edits (seconds)
    EDIT_INTERVAL = 3.0

    def __init__(self, title: str, total_files: int):
        self.title = title
        self.total_files = total_files
        self.files: List[FileProgress] = []
        self.task_start = time.time()
        self._last_edit_time = 0.0
        self._last_text = ""
        self._total_bytes: int = 0

    # ── File Management ──────────────────────────────────────────────────

    def add_file(self, index: int, filename: str, total_bytes: int = 0):
        """Register a file to track."""
        fp = FileProgress(index=index, filename=filename, total_bytes=total_bytes)
        self.files.append(fp)
        self._total_bytes += total_bytes

    def mark_downloading(self, index: int, total_bytes: int = 0):
        """Mark a file as currently downloading."""
        fp = self._get(index)
        fp.status = FileStatus.DOWNLOADING
        fp.total_bytes = total_bytes
        fp.start_time = time.time()
        self._total_bytes = sum(f.total_bytes for f in self.files)

    def update_download(self, index: int, current_bytes: int):
        """Update download progress for a file."""
        fp = self._get(index)
        fp.current_bytes = current_bytes

    def mark_uploading(self, index: int):
        """Mark a file as currently uploading (download complete)."""
        fp = self._get(index)
        fp.status = FileStatus.UPLOADING
        fp.current_bytes = fp.total_bytes  # download done
        fp.start_time = time.time()

    def update_upload(self, index: int, current_bytes: int):
        """Update upload progress for a file."""
        fp = self._get(index)
        fp.current_bytes = current_bytes

    def mark_sent(self, index: int):
        """Mark a file as successfully sent."""
        fp = self._get(index)
        fp.status = FileStatus.SENT
        fp.current_bytes = fp.total_bytes

    def mark_failed(self, index: int, error: str = ""):
        """Mark a file as failed."""
        fp = self._get(index)
        fp.status = FileStatus.FAILED
        fp.error = error

    def mark_skipped(self, index: int):
        """Mark a file as skipped."""
        fp = self._get(index)
        fp.status = FileStatus.SKIPPED

    # ── Message Building ─────────────────────────────────────────────────

    def build_message(self) -> str:
        """Build the progress message for Telegram."""
        lines: List[str] = []
        lines.append(f"📦 **{self.title}**")

        done = sum(1 for f in self.files if f.status in (FileStatus.SENT, FileStatus.SKIPPED))
        failed = sum(1 for f in self.files if f.status == FileStatus.FAILED)
        active = [f for f in self.files if f.status in (FileStatus.DOWNLOADING, FileStatus.UPLOADING)]

        # Overall progress bar
        overall_pct = (done / self.total_files * 100) if self.total_files > 0 else 0
        lines.append(
            f"`[{_progress_bar(overall_pct)}]` {done}/{self.total_files} files"
            + (f" ⚠️ {failed} failed" if failed else "")
        )

        # Speed and ETA from active files
        total_speed = 0.0
        for fp in active:
            spd = self._speed(fp)
            total_speed += spd
            remaining = fp.total_bytes - fp.current_bytes
            eta = remaining / spd if spd > 0 else 0
            op = "⬇️" if fp.status == FileStatus.DOWNLOADING else "⬆️"
            lines.append(
                f"{op} `{fp.filename}` "
                f"`[{_progress_bar(fp.percent)}]` {fp.percent:.0f}% "
                f"• {_fmt_speed(spd)} • ETA {_fmt_eta(eta)}"
            )

        # Overall speed + ETA
        if total_speed > 0:
            total_remaining = sum(f.total_bytes - f.current_bytes for f in active)
            overall_eta = total_remaining / total_speed if total_speed > 0 else 0
            lines.append(f"⚡ {_fmt_speed(total_speed)} • ETA {_fmt_eta(overall_eta)}")

        # Sent files summary
        sent = [f for f in self.files if f.status == FileStatus.SENT]
        if sent and not active:
            for fp in sent:
                lines.append(f"✅ `{fp.filename}`")

        # Failed files summary
        failed_files = [f for f in self.files if f.status == FileStatus.FAILED]
        for fp in failed_files:
            lines.append(f"❌ `{fp.filename}` — {fp.error or 'unknown error'}")

        return "\n".join(lines)

    async def update_message(self, status_msg, force: bool = False, buttons=None) -> bool:
        """
        Edit the Telegram status message if enough time has passed.

        Args:
            status_msg: The Telegram message to edit.
            force: If True, skip rate-limit / dedup checks.
            buttons: Optional inline keyboard buttons (Telethon format,
                     e.g. ``[[Button.inline(...)]]``).

        Returns True if the message was edited.
        """
        now = time.time()
        text = self.build_message()

        if not force and (now - self._last_edit_time < self.EDIT_INTERVAL):
            return False
        if not force and text == self._last_text:
            return False

        self._last_edit_time = now
        self._last_text = text

        try:
            if buttons is not None:
                await status_msg.edit(text, buttons=buttons)
            else:
                await status_msg.edit(text)
            return True
        except TelethonRPCError:
            return False  # Telegram rate-limit or message deleted

    async def finalize(self, status_msg):
        """Force a final message update with complete results."""
        await self.update_message(status_msg, force=True)

    def get_error_summary(self) -> str:
        """Return a summary string of all failed files."""
        failed = [f for f in self.files if f.status == FileStatus.FAILED]
        if not failed:
            return ""
        lines = [f"• `{f.filename}` — {f.error or 'unknown error'}" for f in failed]
        return f"⚠️ **{len(failed)} file(s) failed:**\n" + "\n".join(lines)

    @property
    def has_failures(self) -> bool:
        return any(f.status == FileStatus.FAILED for f in self.files)

    @property
    def sent_count(self) -> int:
        return sum(1 for f in self.files if f.status == FileStatus.SENT)

    @property
    def total_elapsed(self) -> float:
        return time.time() - self.task_start

    # ── Internal ─────────────────────────────────────────────────────────

    def _get(self, index: int) -> FileProgress:
        for fp in self.files:
            if fp.index == index:
                return fp
        raise IndexError(f"No file registered with index {index}")

    def _speed(self, fp: FileProgress) -> float:
        """Calculate current speed in bytes/sec for a file."""
        elapsed = time.time() - fp.start_time
        if elapsed <= 0 or fp.current_bytes <= 0:
            return 0.0
        return fp.current_bytes / elapsed
