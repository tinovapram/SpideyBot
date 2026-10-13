from __future__ import annotations

from types import SimpleNamespace

import pytest

from downloader import bypass
from downloader.bypass import RedirectAdapter, adapter_name, resolve


@pytest.mark.parametrize(
    "url",
    [
        "https://ouo.press/example",
        "https://gplinks.co/example",
        "https://exe.io/example",
        "https://linkvertise.com/example",
        "https://work.ink/example",
        "https://www.lockr.net/example",
    ],
)
def test_known_shortener_families_are_recognised(url: str) -> None:
    assert adapter_name(url) == "redirect"


def test_unsupported_host_is_rejected_before_network() -> None:
    with pytest.raises(ValueError, match="unsupported shortener"):
        resolve("https://example.com/file")


def test_malformed_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="http or https"):
        resolve("not-a-url")


def test_redirect_response_url_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    response = SimpleNamespace(url="https://example.org/final", raise_for_status=lambda: None)

    def fake_get(url: str, **kwargs: object) -> SimpleNamespace:
        calls.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(bypass.requests, "get", fake_get)

    assert RedirectAdapter(timeout=3.5, max_redirects=4).resolve("https://ouo.press/example") == response.url
    assert calls == [{
        "url": "https://ouo.press/example",
        "allow_redirects": True,
        "max_redirects": 4,
        "timeout": 3.5,
        "impersonate": "chrome",
    }]


def test_http_status_failure_is_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailedResponse:
        url = "https://ouo.press/example"

        def raise_for_status(self) -> None:
            raise RuntimeError("server returned 500")

    monkeypatch.setattr(bypass.requests, "get", lambda *args, **kwargs: FailedResponse())

    with pytest.raises(RuntimeError, match="500"):
        RedirectAdapter().resolve("https://ouo.press/example")


def test_timeout_is_passed_to_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_get(*args: object, **kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        raise TimeoutError("timed out")

    monkeypatch.setattr(bypass.requests, "get", fake_get)

    with pytest.raises(TimeoutError, match="timed out"):
        RedirectAdapter(timeout=1.25).resolve("https://ouo.press/example")
    assert captured["timeout"] == 1.25


def test_redirect_adapter_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="timeout"):
        RedirectAdapter(timeout=0)
    with pytest.raises(ValueError, match="max_redirects"):
        RedirectAdapter(max_redirects=0)
