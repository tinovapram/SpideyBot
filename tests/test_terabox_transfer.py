"""Tests for the multi-backend TeraBox transfer engine.

Exercises backend selection, aria2 readout parsing, and byte-for-byte
integrity of ``single_stream_download`` and ``segmented_download`` against a
local Range-capable HTTP server. ``aria2_download`` runs only when the
``aria2c`` binary is available (auto-skipped otherwise).
"""

from __future__ import annotations

import hashlib
import os
import re

import aiohttp
import pytest
from aiohttp import web

from core import config
from downloader import terabox_transfer as tt


# ── helpers ─────────────────────────────────────────────────────────

def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_range_app(data: bytes):
    async def handler(request: web.Request) -> web.Response:
        length = len(data)
        range_header = request.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if match:
                start = int(match.group(1))
                end_raw = match.group(2)
                end = int(end_raw) if end_raw else length - 1
                end = min(end, length - 1)
                if start > end:
                    return web.Response(status=416)
                body = data[start : end + 1]
                return web.Response(
                    body=body,
                    status=206,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{length}",
                        "Content-Length": str(len(body)),
                        "Accept-Ranges": "bytes",
                    },
                )
        return web.Response(body=data)

    app = web.Application()
    app.router.add_get("/file", handler)
    return app


async def _start_server(data: bytes) -> str:
    runner = web.AppRunner(_make_range_app(data))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}/file", runner


# ── backend selection ───────────────────────────────────────────────

class TestPickBackend:
    def test_explicit_modes(self, monkeypatch):
        for mode in ("aria2", "segmented", "single"):
            monkeypatch.setattr(config, "TERABOX_TRANSFER", mode)
            assert tt.pick_transfer_backend(1024**3) == mode

    def test_auto_large_segmented_when_available(self, monkeypatch):
        monkeypatch.setattr(config, "TERABOX_TRANSFER", "auto")
        monkeypatch.setattr(tt, "aria2_available", lambda: True)
        # auto now prefers native aiohttp segmented over aria2.
        assert tt.pick_transfer_backend(1024**3) == "segmented"

    def test_auto_small_file_single(self, monkeypatch):
        monkeypatch.setattr(config, "TERABOX_TRANSFER", "auto")
        monkeypatch.setattr(tt, "aria2_available", lambda: True)
        assert tt.pick_transfer_backend(1 * 1024 * 1024) == "single"

    def test_auto_no_aria2_segmented(self, monkeypatch):
        monkeypatch.setattr(config, "TERABOX_TRANSFER", "auto")
        monkeypatch.setattr(tt, "aria2_available", lambda: False)
        assert tt.pick_transfer_backend(1024**3) == "segmented"


# ── aria2 readout parsing ───────────────────────────────────────────

class TestParseReadout:
    def test_percent(self):
        assert tt._parse_readout("[#ab12 512MiB/1.0GiB(50%) CN:8 DL:2MiB]", 1024**3) == 512 * 1024**2

    def test_done_total_bytes(self):
        line = "[#ab12 30MiB/120MiB(25%) CN:4 DL:1.2MiB ETA:1m]"
        assert tt._parse_readout(line, 120 * 1024**2) == 30 * 1024**2

    def test_garbage(self):
        assert tt._parse_readout("no progress here", 1000) is None


# ── transfer integrity (local Range server) ────────────────────────

async def _download_and_verify(download_fn, data: bytes, filepath: str):
    progress: list[int] = []
    done = await download_fn(
        filepath,
        expected_size=len(data),
        progress_callback=lambda fname, cur, tot: progress.append(cur),
    )
    assert os.path.exists(done)
    assert os.path.getsize(done) == len(data)
    assert _sha(done) == hashlib.sha256(data).hexdigest()
    assert progress and progress[-1] == len(data)
    return done


class TestSingleStream:
    async def test_byte_identical(self, tmp_path):
        data = os.urandom(3 * 1024 * 1024 + 12345)
        url, runner = await _start_server(data)
        try:
            filepath = str(tmp_path / "single.bin")
            await _download_and_verify(
                lambda fp, **kw: tt.single_stream_download(
                    url, fp, headers={"User-Agent": "test"}, stall_timeout=30, **kw
                ),
                data,
                filepath,
            )
        finally:
            await runner.cleanup()


class TestSegmented:
    async def test_byte_identical_multipart(self, tmp_path):
        data = os.urandom(5 * 1024 * 1024 + 777)  # > 4 parts of ~1 MiB
        url, runner = await _start_server(data)
        try:
            filepath = str(tmp_path / "seg.bin")
            await _download_and_verify(
                lambda fp, **kw: tt.segmented_download(
                    url, fp, connections=8, stall_timeout=30, **kw
                ),
                data,
                filepath,
            )
            # part files must be cleaned up
            leftovers = [n for n in os.listdir(tmp_path) if n.startswith("seg.bin.part")]
            assert leftovers == []
        finally:
            await runner.cleanup()

    async def test_falls_back_when_no_range(self, tmp_path):
        """A server ignoring Range (200) must raise -> caller falls back."""
        async def handler(request):
            return web.Response(body=b"x" * 1024)

        app = web.Application()
        app.router.add_get("/plain", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/plain"
        try:
            with pytest.raises(tt.TransferError):
                await tt.segmented_download(url, str(tmp_path / "x.bin"), connections=4)
        finally:
            await runner.cleanup()


class TestAria2:
    @pytest.mark.skipif(not tt.aria2_available(), reason="aria2c not installed")
    async def test_byte_identical(self, tmp_path):
        data = os.urandom(4 * 1024 * 1024 + 999)
        url, runner = await _start_server(data)
        try:
            filepath = str(tmp_path / "aria.bin")
            await _download_and_verify(
                lambda fp, **kw: tt.aria2_download(
                    url, fp, connections=4, stall_timeout=30, **kw
                ),
                data,
                filepath,
            )
        finally:
            await runner.cleanup()
