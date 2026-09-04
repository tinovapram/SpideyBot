"""Human-readable formatting helpers (sizes, speeds, ETA, progress bars)."""

from __future__ import annotations

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_size(num_bytes: float) -> str:
    """Format a byte count into a human-readable string."""
    value = float(num_bytes)
    for unit in _SIZE_UNITS:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def format_speed(bps: float) -> str:
    """Format bytes/sec into a human-readable speed."""
    if bps <= 0:
        return "---"
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / (1024 * 1024):.1f} MB/s"


def format_eta(seconds: float) -> str:
    """Format seconds into a human-readable ETA."""
    if seconds <= 0 or seconds > 86400:
        return "---"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}m {secs}s"
    hours, rem = divmod(int(seconds), 3600)
    minutes, _ = divmod(rem, 60)
    return f"{hours}h {minutes}m"


def progress_bar(percent: float, width: int = 10) -> str:
    """Render a Unicode progress bar."""
    filled = int(max(0.0, min(100.0, percent)) / 100 * width)
    return "█" * filled + "░" * (width - filled)
