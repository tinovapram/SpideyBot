"""Link-shortener resolution (FR-18).

Shortener resolution is deliberately separate from media-site adapters: it returns a final URL,
which the normal downloader pipeline can then process.
"""

from __future__ import annotations

import re
from typing import Protocol

from curl_cffi import requests


class ShortenerAdapter(Protocol):
    """Resolve one recognised shortener family."""

    def resolve(self, url: str) -> str: ...


class RedirectAdapter:
    """Conservative redirect resolver for shorteners that expose HTTP redirects."""

    def __init__(self, *, timeout: float = 8.0, max_redirects: int = 10) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_redirects < 1:
            raise ValueError("max_redirects must be positive")
        self.timeout = timeout
        self.max_redirects = max_redirects

    def resolve(self, url: str) -> str:
        response = requests.get(
            url,
            allow_redirects=True,
            max_redirects=self.max_redirects,
            timeout=self.timeout,
            impersonate="chrome",
        )
        response.raise_for_status()
        return str(response.url)


# Families that are safe to resolve by following ordinary HTTP redirects.
# Gate-specific hosts are intentionally not included until they have a
# dedicated adapter for their JavaScript/API protocol.
_REDIRECT_HOSTS = frozenset({
    "ouo.press", "ouo.io", "ouo.link", "adf.ly", "adfoc.us",
    "cuty.io", "cuty.me", "cuttty.com", "exe.io", "exeygo.com",
    "lksfy.com", "linkshortify.com", "rinku.me", "7mb.io",
    "boostellar.com", "bstlar.com", "bstshrt.com", "boostylink.com",
    "keyforge.com", "linkunlocker.net", "cut4money.com", "adurl.io", "shr2.link",
    "droplink.co", "linkvertise.com", "gplinks.co", "gplinks.com", "gplinks.in",
    "shrinkme.io", "shrinkearn.com", "oii.la",
    "tpi.li", "aii.sh", "lnbz.la", "1shortlink.com", "arolinks.com", "vplink.in",
    "linksterr.com", "linkjust.com", "liteshort.com", "shortxlinks.com", "genlink.io",
    "icutlink.com", "fc-lc.xyz", "fc.lc", "oii.io", "bbmkts.com", "linknext.co",
    "shorte.io", "tfly.cc", "mitly.us", "linclik.com", "cpm.link", "cpmlink.net",
    "shrtslug.com", "ez4short.com", "game5s.com", "tech8s.com", "work.ink",
    "link4m.com", "lockr.net", "lockr.so", "lockr.to", "sfl.gl", "earnlinks.com",
    "linksgo.in", "earn4link.in", "ontops.link", "clipi.cc", "nitro-link.com",
    "multiup.io", "filepress.baby", "molyn.top",
})

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(r"^https?://(?:www\.)?" + re.escape(host) + r"(?:/|$)", re.IGNORECASE), "redirect")
    for host in sorted(_REDIRECT_HOSTS, key=len, reverse=True)
)


def adapter_name(url: str) -> str | None:
    """Return the recognised family strategy, or ``None`` for unsupported URLs."""
    for pattern, name in _PATTERNS:
        if pattern.match(url.strip()):
            return name
    return None


def resolve(url: str, *, adapter: ShortenerAdapter | None = None) -> str:
    """Resolve a supported shortener URL with bounded network timeouts.

    Unsupported URLs and malformed values fail transparently instead of being silently
    treated as successfully bypassed.
    """
    cleaned = url.strip()
    if not re.match(r"^https?://", cleaned, re.IGNORECASE):
        raise ValueError("URL must use http or https")
    if adapter_name(cleaned) is None:
        raise ValueError("unsupported shortener")
    return (adapter or RedirectAdapter()).resolve(cleaned)


__all__ = ["RedirectAdapter", "ShortenerAdapter", "adapter_name", "resolve"]
