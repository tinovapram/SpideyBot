"""A real HTTP server for tests that are about the transport (D25, D31).

Fetching is the one part of the pipeline whose behaviour *is* the network: whether headers arrive,
whether a cap stops a transfer mid-body, whether a truncated response is noticed. A stubbed
transport would assert what I believe curl_cffi does rather than what it does — and it would have
missed three findings already: ``chunk_size`` is ignored, a short body raises ``IncompleteRead``,
and impersonation supplies a ``User-Agent`` while explicit headers override the rest of the profile.

The server is HTTP/1.0 on purpose: every response ends by closing the connection, so a body with no
``Content-Length`` is delimited by the close, which is exactly how a host that refuses to measure
itself behaves. It also records the headers it received, so a test can assert what actually went out
rather than what we intended to send.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    #: Where ``/file/...`` is served from. Tests that serve generated media point this elsewhere.
    root: Path
    #: Every request's headers, in arrival order.
    requests: list[dict[str, str]]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: object) -> None:
        """Silence the default stderr logging. A test run is not a server log."""

    def do_GET(self) -> None:  # noqa: N802 - the name is the protocol
        server: FixtureServer = self.server  # type: ignore[assignment]
        server.requests.append(dict(self.headers.items()))

        route, _, argument = self.path.lstrip("/").partition("/")
        try:
            if route == "file":
                self.send_body(200, (server.root / argument).read_bytes())
            elif route == "padded":
                self.send_generated(int(argument), measured=True)
            elif route == "unmeasured":
                self.send_generated(int(argument), measured=False)
            elif route == "empty":
                self.send_body(200, b"")
            elif route == "missing":
                self.send_body(404, b"gone")
            elif route == "truncate":
                self.send_truncated()
            elif route == "slow":
                self.send_slowly(int(argument))
            else:
                self.send_body(404, b"unknown route")
        except BrokenPipeError, ConnectionResetError:
            # Expected whenever a test asserts that a transfer was stopped early.
            self.close_connection = True

    def send_body(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_generated(self, size: int, *, measured: bool) -> None:
        """A body of ``size`` bytes, with or without a ``Content-Length`` to announce it."""
        self.send_response(200)
        if measured:
            self.send_header("Content-Length", str(size))
        self.end_headers()
        block = b"z" * 65536
        remaining = size
        while remaining > 0:
            piece = block[: min(len(block), remaining)]
            self.wfile.write(piece)
            remaining -= len(piece)

    def send_truncated(self) -> None:
        """Promise more than is sent, then hang up: a stream that dies mid-body."""
        self.send_response(200)
        self.send_header("Content-Length", "10000")
        self.end_headers()
        self.wfile.write(b"y" * 100)
        self.wfile.flush()
        self.close_connection = True

    def send_slowly(self, size: int) -> None:
        """Send half now and half after a pause, so a client can be caught mid-transfer."""
        half = size // 2
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        self.wfile.write(b"s" * half)
        self.wfile.flush()
        time.sleep(0.4)
        self.wfile.write(b"s" * (size - half))


@pytest.fixture
def server(tmp_path: Path) -> Iterator[FixtureServer]:
    """A fixture server rooted at ``tmp_path``; ``/file/<name>`` serves what a test writes there."""
    httpd = FixtureServer(("127.0.0.1", 0), _Handler)
    httpd.root = tmp_path
    httpd.requests = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    """A destination directory that is deliberately *not* the server's root.

    Downloading a file onto the very path the server is reading it from would truncate it before the
    response was sent — a race that silently turns a transfer test into a zero-byte test.
    """
    return tmp_path / "staging"


def url(server: FixtureServer, path: str) -> str:
    """The loopback URL for a route, so no host other than 127.0.0.1 is ever contacted."""
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/{path}"
