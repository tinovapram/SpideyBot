"""
Unified live Telegram progress message for downloads and uploads.

A single :class:`StatusMessage` is bound to one Telegram status message and
drives every phase of a task -- site download, Telegram download, Telegram
upload -- through the *same* renderer. The user therefore sees one consistent
message that evolves (header + live rows + cancel footer) instead of a stream
of differently-formatted ad-hoc edits.

Callback context
----------------
Telethon's ``upload_file`` / ``send_file`` and ``download_media`` invoke their
``progress_callback`` on the event loop with ``(current, total)``. HTTP site
downloaders call the same signature from executor worker threads.
:meth:`StatusMessage.bytes_cb` returns a single callback that works for both:
:class:`StatusMessage` detects whether it was called from the event loop or a
worker thread and schedules a rate-limited, flood-safe edit accordingly.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

from utils.format import format_size, progress_bar
from utils.telethon import safe_edit


def bytes_line(icon: str, label: str, done: float, total: float) -> str:
    """A consistent single-line byte-progress row (e.g. ``📥 Downloading``)."""
    done = max(0.0, float(done))
    if total <= 0:
        return f"{icon} **{label}:** {format_size(done)}"
    percent = min(100.0, done * 100.0 / float(total))
    bar = progress_bar(percent)
    return (
        f"{icon} **{label}:** `[{bar}]` {percent:.0f}% "
        f"({format_size(done)} / {format_size(total)})"
    )


def count_line(icon: str, label: str, current: int, total: Optional[int] = None) -> str:
    """A consistent count row (e.g. ``📤 Uploaded: 3/5``)."""
    if total:
        return f"{icon} **{label}:** {current}/{total}"
    return f"{icon} **{label}:** {current}"


class StatusMessage:
    """One live Telegram status message with unified, rate-limited rendering.

    - ``header`` -- title line kept on top while the task runs.
    - named ``rows`` -- replaceable live lines (bytes / counts / phases).
    - ``footer`` -- persistent hint (e.g. the ``/cancel`` line).

    All public mutators are thread-safe: call them from the event loop or from
    worker threads. Edits are coalesced and rate-limited to avoid flooding
    Telegram. While a transfer row is present the header also receives an
    animated ellipsis (``Downloading.`` → ``Downloading..`` → ``Downloading...``)
    so the user can see the task is alive even when the byte counter stalls.
    """

    RENDER_INTERVAL = 3.0  # minimum gap between edits caused by content change
    ANIM_INTERVAL = 1.2    # minimum gap between liveness-only edits (stuck progress)
    PULSE_INTERVAL = 0.6   # how often the renderer re-checks while a row is active

    def __init__(self, message, *, header: str = "", footer: str = "") -> None:
        self._message = message
        self._header = header
        self._footer = footer
        self._rows: dict[str, str] = {}
        self._lock = threading.Lock()
        self._in_flight = False
        self._closed = False
        self._last_edit = 0.0
        self._last_text = ""
        self._last_body = ""
        self._render_future = None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # ── Public sync API (thread-safe) ─────────────────────────────

    def set_header(self, text: str) -> None:
        """Replace the title line shown above the live rows."""
        self._header = text
        self._kick()

    def row(self, name: str, text: str) -> None:
        """Set or replace a named live row. An empty *text* drops the row."""
        if text:
            self._rows[name] = text
        else:
            self._rows.pop(name, None)
        self._kick()

    def drop(self, name: str) -> None:
        """Remove a named live row."""
        self._rows.pop(name, None)
        self._kick()

    def bytes_cb(self, name: str, icon: str, label: str) -> Callable[[int, int], None]:
        """Return a ``cb(current, total)`` callback for a byte-progress phase.

        Works unchanged for Telethon ``upload_file`` / ``send_file``, Telethon
        ``download_media``, and HTTP site downloaders (thread or event loop).
        """
        def _cb(current, total) -> None:
            self.row(name, bytes_line(icon, label, current, total or 0))
        return _cb

    def count_cb(
        self,
        name: str,
        icon: str,
        label: str,
        total: Optional[int] = None,
    ) -> Callable[[int, Optional[int]], None]:
        """Return a ``cb(current, [total])`` callback for a counting phase."""
        def _cb(current, known_total=None) -> None:
            self.row(
                name,
                count_line(icon, label, current, total if total is not None else known_total),
            )
        return _cb

    # ── Public async API ──────────────────────────────────────────

    async def close(self, text: str) -> bool:
        """Replace the message with final *text* and stop further updates."""
        self._closed = True
        with self._lock:
            future = self._render_future
        if future is not None and not future.done():
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(future), timeout=self.PULSE_INTERVAL + 0.5
                )
            except asyncio.TimeoutError:
                future.cancel()
            except Exception:
                pass
        return await safe_edit(self._message, text)

    async def render_now(self) -> None:
        """Force an immediate render of the current header/rows/footer."""
        text = self._compose()
        await safe_edit(self._message, text)
        self._last_edit = time.time()
        self._last_text = text
        self._last_body = self._body()

    # ── Internal ──────────────────────────────────────────────────

    def _parts(self) -> list:
        parts = []
        if self._header:
            parts.append(self._header)
        parts.extend(self._rows.values())
        return parts

    def _with_footer(self, text: str) -> str:
        if self._footer:
            return f"{text}\n\n{self._footer}" if text else self._footer
        return text

    def _body(self) -> str:
        """Header + rows + footer, without the animated liveness dots."""
        return self._with_footer("\n".join(self._parts()))

    def _dots(self) -> str:
        """Animated ellipsis that advances every second while a row is live."""
        return "." * (int(time.time()) % 3 + 1)

    def _compose(self) -> str:
        parts = self._parts()
        if self._rows and not self._closed and parts:
            parts[0] = f"{parts[0]}{self._dots()}"
        return self._with_footer("\n".join(parts))

    def _kick(self) -> None:
        """Start (or keep) the pulse renderer running while rows are live."""
        if self._closed:
            return
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
                self._loop = loop
            except RuntimeError:
                return
        if not loop.is_running():
            return
        with self._lock:
            if self._in_flight:
                return
            self._in_flight = True
        future = asyncio.run_coroutine_threadsafe(self._render_loop(), loop)
        with self._lock:
            self._render_future = future

    async def _render_loop(self) -> None:
        """Render immediately, then pulse while any transfer row is live.

        Edits are throttled: a content change waits ``RENDER_INTERVAL``; when
        the content is unchanged (stuck bytes), a liveness edit still fires
        every ``ANIM_INTERVAL`` so the animated dots keep moving.
        """
        try:
            if not self._closed:
                await self._render_pass()
            while not self._closed and self._rows:
                await asyncio.sleep(self.PULSE_INTERVAL)
                if not self._closed:
                    await self._render_pass()
        finally:
            with self._lock:
                self._in_flight = False

    async def _render_pass(self) -> None:
        now = time.time()
        content_changed = self._body() != self._last_body
        min_gap = self.RENDER_INTERVAL if content_changed else self.ANIM_INTERVAL
        if now - self._last_edit < min_gap:
            return
        text = self._compose()
        if text == self._last_text:
            return
        if await safe_edit(self._message, text):
            self._last_edit = now
            self._last_text = text
            self._last_body = self._body()
