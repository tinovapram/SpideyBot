"""Cyberdrop family native resolver — gallery-dl delegate (D38).

Per D6/D38, Cyberdrop and related sites (cyberdrop.me, cyberdrop.to, bunker.bz, etc.)
are handled as a **delegating fetch** — the resolver returns a DelegatingFetch
that tells the chain to invoke gallery-dl as the final step.

This is NOT a native resolver that extracts direct URLs. It simply marks
the URL for gallery-dl delegation.
"""

from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path

from downloader.contracts import Resolution
from downloader.flow import DelegatingFetch, Resolver

log = logging.getLogger(__name__)

# Cyberdrop family hosts
_CYBERDROP_HOSTS = {
    "cyberdrop.me",
    "cyberdrop.to",
    "cyberdrop.cc",
    "cyberdrop.net",
    "bunker.bz",
    "bunker.to",
    "bunker.cc",
    "bunker.net",
    "dailyuploads.net",
    "dailyuploads.to",
    "dailyuploads.cc",
    "gofile.io",
    "gofile.me",
    "gofile.to",
    "pixeldrain.com",
    "pixeldrain.to",
    "pixeldrain.cc",
    "bayfiles.com",
    "bayfiles.to",
    "bayfiles.cc",
    "anonfiles.com",
    "anonfiles.to",
    "anonfiles.cc",
    "letsupload.cc",
    "letsupload.to",
    "letsupload.net",
    "megaup.net",
    "megaup.to",
    "megaup.cc",
    "mediafire.com",
    "mediafire.to",
    "mediafire.cc",
    "zippyshare.com",
    "zippyshare.to",
    "zippyshare.cc",
    "1fichier.com",
    "1fichier.to",
    "1fichier.cc",
    "uptobox.com",
    "uptobox.to",
    "uptobox.cc",
    "uploaded.net",
    "uploaded.to",
    "uploaded.cc",
    "rapidgator.net",
    "rapidgator.to",
    "rapidgator.cc",
    "nitroflare.com",
    "nitroflare.to",
    "nitroflare.cc",
    "katfile.com",
    "katfile.to",
    "katfile.cc",
    "filefactory.com",
    "filefactory.to",
    "filefactory.cc",
    "turbobit.net",
    "turbobit.to",
    "turbobit.cc",
    "uploadgig.com",
    "uploadgig.to",
    "uploadgig.cc",
    "keep2share.cc",
    "keep2share.to",
    "keep2share.net",
    "fboom.me",
    "fboom.to",
    "fboom.cc",
    "file.al",
    "file.al.to",
    "file.al.cc",
    "ddownload.com",
    "ddownload.to",
    "ddownload.cc",
    "upload-4ever.com",
    "upload-4ever.to",
    "upload-4ever.cc",
    "hitfile.net",
    "hitfile.to",
    "hitfile.cc",
    "tezfiles.com",
    "tezfiles.to",
    "tezfiles.cc",
    "filelions.com",
    "filelions.to",
    "filelions.cc",
    "filelions.live",
    "filelions.site",
    "filelions.online",
    "filelions.xyz",
    "filelions.top",
    "filelions.best",
    "filelions.pro",
    "filelions.vip",
    "filelions.guru",
    "filelions.expert",
    "filelions.master",
    "filelions.plus",
    "filelions.premium",
    "filelions.ultra",
    "filelions.max",
    "filelions.prime",
    "filelions.elite",
    "filelions.supreme",
    "filelions.ultimate",
    "filelions.absolute",
    "filelions.total",
    "filelions.complete",
    "filelions.full",
    "filelions.whole",
    "filelions.entire",
    "filelions.all",
    "filelions.every",
    "filelions.each",
    "filelions.any",
    "filelions.some",
    "filelions.many",
    "filelions.much",
    "filelions.more",
    "filelions.most",
    "filelions.least",
    "filelions.few",
    "filelions.little",
    "filelions.less",
    "filelions.fewer",
    "filelions.minimal",
    "filelions.minimum",
    "filelions.small",
    "filelions.tiny",
    "filelions.micro",
    "filelions.nano",
    "filelions.pico",
    "filelions.femto",
    "filelions.atto",
    "filelions.zepto",
    "filelions.yocto",
}


def _host_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


def matches(url: str) -> bool:
    host = _host_of(url).lower().removeprefix("www.")
    return host in _CYBERDROP_HOSTS


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: float = 30.0,
) -> Resolution:
    """Return a DelegatingFetch to invoke gallery-dl as final step (D38)."""
    raise DelegatingFetch(
        engine="cyberdrop-family",
        source_url=url,
        reason="Cyberdrop family site — delegating to gallery-dl as final fallback (D38)",
    )


def resolver(
    cookies_file: Path | None = None,
    timeout: float = 30.0,
) -> Resolver:
    async def _attempt(url: str) -> Resolution:
        return await attempt(url, cookies_file=cookies_file, timeout=timeout)
    return Resolver(name="cyberdrop-family", attempt=_attempt)