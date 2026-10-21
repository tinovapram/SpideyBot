from __future__ import annotations

import pytest

from utils.urls import is_obviously_private_request, validate_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/file.mp4",
        "http://localhost:8080/file.mp4",
        "http://[::1]/file.mp4",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/file.mp4",
        "http://192.168.1.10/file.mp4",
        "http://user:password@example.com/file.mp4",
        "ftp://example.com/file.mp4",
    ],
)
async def test_private_or_unsupported_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        await validate_public_url(url)


async def test_public_ip_literal_is_allowed() -> None:
    # A literal avoids depending on DNS while still exercising the accept path.
    url = "https://93.184.216.34/file.mp4"

    assert await validate_public_url(url) == url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1/x", True),
        ("http://localhost/x", True),
        ("http://[::1]/x", True),
        ("data:image/png;base64,AAAA", False),
        ("https://example.com/x", False),
    ],
)
def test_browser_subrequest_guard(url: str, expected: bool) -> None:
    assert is_obviously_private_request(url) is expected
