"""TeraBox native resolver — share inventory via the real API, and an honest refusal otherwise.

What the probes established, and what this module therefore does:

  /s/{short} or /sharing/link?surl={short}
    → scrape the `jsToken` from the share page (anonymous — a session cookie makes the page
      redirect-loop, and makes the listing answer `errno 4000020`)
    → `dm.terabox.app/share/list` anonymously, recursing folders (`root=1`, then `root=0&dir=…`)
      for the file inventory: `server_filename`, `size`, `fs_id`, `path`
    → if the listing carried signed `dlink`s, return them as single-file transfers (V4's Method A;
      it no longer does for current shares, so this is a cheap check rather than the path)
    → otherwise ask *why*: with a cookie, one file's link is requested only to read TeraBox's
      answer, and that answer becomes the user-facing reason

The reason matters, because the honest one is not "no files found": TeraBox now refuses to issue a
download link until the account completes its `need verify_v2` check (`errno 400810` on
`/share/transfer`, `errno 400310` on `/share/download`). That is server-side policy, and it is why
the account half below is ported from V4 rather than merely described: it is what will resolve a
share once that gate is satisfied, or on any share TeraBox does not gate.

Cookies: the account's `ndus` is what is needed, and only for the account API — `/main` (to scrape
`bdstoken`), `/api/create`, `/share/transfer`, `/api/filemetas`, `/api/streaming`. V4's own rule was
that a valid cookie merely contains `ndus=`; `ndut` is not required. The caller supplies it as a
string via `ChainContext` (D8, FR-14), or as a legacy cookie file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

from curl_cffi import requests

from downloader.contracts import (
    Resolution,
    ResolvedFile,
    ResolveError,
    Transfer,
    TransferRole,
    kind_for,
    safe_filename,
    unique_name,
)
from downloader.flow import Resolver
from downloader.sites import bridge

log = logging.getLogger(__name__)

# Hosts TeraBox answers on. Suffix matching, so `m.terabox.com` and the regional mirrors both work
# and a lookalike does not (`terabox.com.evil.test`).
#
# `1024tera.com` is the omission that made a reported share fall through to gallery-dl: the list had
# `1024terabox.com`, which looks like the same thing and is not. V4 listed `www.1024tera.com`
# explicitly and V5 matched it as an exact host, so neither had this gap; suffix matching covers the
# bare and `www.` forms at once.
_HOSTS = (
    "terabox.com",
    "terabox.app",
    "terabox.link",
    "terabox.jp",
    "terabox.cn",
    "1024terabox.com",
    "1024tera.com",
    "terasharelink.com",
    "freeterabox.com",
)

#: Where the share API lives. Distinct from `_HOSTS`: a *user's* link is on a share host, while the
#: listing call goes to an API host, and the two are not interchangeable (the share page and the API
#: need different cookies and different headers).
_API_HOST = "dm.terabox.app"
#: The share page to scrape the `jsToken` from. Any `_HOSTS` entry with `www.` works; this is the
#: one measured to answer without a redirect loop.
_SHARE_HOST = "www.1024tera.com"

# The share page embeds the API token inside an encoded `fn("…")` call. These are V4's patterns; the
# first is the one measured to work, and it has to stay lazy — a `[^%]+` variant matches a longer
# span and the API answers `errno 4000020` for the result.
_JSTOKEN_PATTERNS = (
    re.compile(r"fn%28%22(.*?)%22%29"),
    re.compile(r'fn\("([0-9A-Fa-f]{32,})"\)'),
    re.compile(r'jsToken\s*=\s*["\']([^"\']+)["\']'),
    re.compile(r'jsToken["\']?\s*:\s*["\']([^"\']+)["\']'),
    re.compile(r'window\.jsToken\s*=\s*["\']([^"\']+)["\']'),
)

#: The account's own token, scraped from `/main`. The page is URL-*encoded*, so it must be unquoted
#: before this can match — the omission that once made a live cookie look expired.
_BDSTOKEN_RE = re.compile(r'bdstoken["\']?\s*[:=]\s*["\']([a-f0-9]{32})["\']', re.IGNORECASE)

DEFAULT_TIMEOUT = bridge.DEFAULT_TIMEOUT


from downloader.terabox import TeraBoxDownloader, TeraBoxError


def matches(url: str) -> bool:
    """Whether this resolver handles the link."""
    return bridge.host_matches(url, _HOSTS)


async def attempt(
    url: str,
    *,
    cookies_file: Path | None = None,
    cookies: str | None = None,
    options: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> Resolution:
    """Resolve a TeraBox share using the V5 TeraBoxDownloader engine."""
    if cookies is None and options:
        cookies = options.get("cookies")
    cookie = _cookie_string(cookies, cookies_file)

    if not cookie:
        raise ResolveError(
            "TeraBox only returns download links to a logged-in account: set your TeraBox "
            "cookie with /setting"
        )

    try:
        async with TeraBoxDownloader(cookie=cookie, timeout=int(timeout)) as downloader:
            result = await downloader.resolve(url, mode="download")
            if not result.ok or not result.files:
                raise ResolveError(result.error or "TeraBox returned no files for this share")

            resolved_files = []
            for f in result.files:
                if not f.dlink:
                    continue
                resolved_files.append(
                    ResolvedFile(
                        filename=safe_filename(f.filename),
                        media_kind=kind_for(f.filename),
                        filesize=f.size_bytes,
                        source_url=url,
                        transfers=(Transfer(url=f.dlink, filesize=f.size_bytes),)
                    )
                )

            if not resolved_files:
                raise ResolveError("TeraBox returned no download links")

            return Resolution(engine="terabox", source_url=url, files=tuple(resolved_files))
    except ResolveError:
        raise
    except Exception as exc:
        raise ResolveError(f"TeraBox resolution failed: {exc}") from exc


#: Ported from V4's `parse_surl`. The cleaning is not cosmetic: TeraBox hands out several URL forms,
#: a user can paste the bare id, and some shares carry a leading `1` the API wants without it.
_SURL_MIN_LEN = 8
_LEADING_ONE_MAX_STRIPS = 4
_VALID_SURL = re.compile(r"^[A-Za-z0-9_-]+$")


def _parse_surl(url: str) -> str:
    """The share id, cleaned, from any form TeraBox hands out.

    Faithful to V4's ``parse_surl``: same markers, same leading-``1`` stripping, same character and
    length checks. It raises :class:`ResolveError` where V4 raised ``TeraBoxURLError``, because
    ``ResolveError`` is the failure the chain understands.
    """
    if "/api/shorturlinfo?" in url:
        surl = url.split("/api/shorturlinfo?", 1)[1].split("&", 1)[0].split("#", 1)[0].strip()
    elif "surl=" in url:
        surl = url.split("surl=", 1)[1].split("&", 1)[0]
    elif "/s/" in url:
        surl = url.split("/s/", 1)[1].split("?", 1)[0].split("#", 1)[0]
    else:
        stripped = url.strip()
        if "://" in stripped or "/" in stripped or "." in stripped:
            raise ResolveError("No TeraBox share id in this link")
        surl = stripped if _VALID_SURL.match(stripped) and len(stripped) >= _SURL_MIN_LEN else ""

    if not surl:
        raise ResolveError("No TeraBox share id in this link")

    surl = surl.rstrip("/").split("/")[-1]
    if not _VALID_SURL.match(surl):
        raise ResolveError(f"TeraBox share id {surl!r} contains characters that cannot be valid")

    if len(surl) > 22 and surl.startswith("1"):
        for _ in range(_LEADING_ONE_MAX_STRIPS):
            if not surl.startswith("1") or len(surl) - 1 < _SURL_MIN_LEN or len(surl) <= 22:
                break
            surl = surl[1:]

    if len(surl) < _SURL_MIN_LEN:
        raise ResolveError("TeraBox share id is too short to be valid")
    return surl


def _surl_candidates(surl: str) -> tuple[str, ...]:
    """The id, then V4's ``1``-prefixed variant.

    V4 tried both for the share root and used whichever worked for every subfolder, because which
    form the API accepts varies by share — `parse_surl` strips a leading ``1`` for the same reason.
    """
    prefixed = surl if surl.startswith("1") else "1" + surl
    return (surl,) if prefixed == surl else (surl, prefixed)


def _is_dir(item: dict[str, Any]) -> bool:
    """`isdir` arrives as an int on some responses and a string on others."""
    return str(item.get("isdir")) == "1"


def _api_headers(surl: str, cookie: str | None = None) -> dict[str, str]:
    """What the share API expects, because its own page calls it as an XHR."""
    headers = {
        "User-Agent": bridge.USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": f"https://{_API_HOST}",
        "Referer": f"https://{_API_HOST}/sharing/link?surl={surl}&clearCache=1",
    }
    if cookie:
        headers["Cookie"] = cookie if "=" in cookie else f"ndus={cookie}"
    return headers


def _find_token(html: str) -> str | None:
    decoded = urllib.parse.unquote(html)
    for pattern in _JSTOKEN_PATTERNS:
        match = pattern.search(decoded) or pattern.search(html)
        if match and match.group(1):
            return match.group(1)
    return None


async def _share_token(session: requests.AsyncSession, surl: str, *, timeout: float) -> str:
    """Scrape the token the listing API demands. Without it the call is refused."""
    try:
        response = await session.get(
            f"https://{_SHARE_HOST}/sharing/link",
            params={"surl": surl},
            headers={"User-Agent": bridge.USER_AGENT},
            timeout=timeout,
        )
    except Exception as failed:
        raise ResolveError(f"Could not reach TeraBox: {failed}") from failed

    token = _find_token(response.text)
    if not token:
        raise ResolveError(
            "TeraBox returned no share token: the share may be private, expired, or removed"
        )
    return token


async def _list(
    session: requests.AsyncSession,
    token: str,
    surl: str,
    *,
    root: int,
    folder: str | None = None,
    cookie: str | None = None,
    timeout: float,
) -> dict[str, Any]:
    """One listing call. ``root=1`` lists the share root; ``root=0`` needs a ``dir``."""
    params = {
        "app_id": "250528",
        "jsToken": token,
        "site_referer": "https://www.terabox.app/",
        "shorturl": surl,
        "root": str(root),
        "order": "time",
        "desc": "1",
        "num": "100",
        "page": "1",
    }
    if folder is not None:
        params["dir"] = folder

    try:
        response = await session.get(
            f"https://{_API_HOST}/share/list",
            params=params,
            headers=_api_headers(surl, cookie),
            timeout=timeout,
        )
        data = response.json()
    except Exception as failed:
        raise ResolveError(f"TeraBox share listing failed: {failed}") from failed

    if data.get("errno") != 0:
        raise ResolveError(f"TeraBox refused the share listing (errno {data.get('errno')})")
    return data


async def _root_listing(
    session: requests.AsyncSession, token: str, surl: str, cookie: str | None = None, *, timeout: float
) -> tuple[dict[str, Any], str]:
    """The share root, trying V4's two candidate ids."""
    failure: ResolveError | None = None
    for candidate in _surl_candidates(surl):
        try:
            return await _list(session, token, candidate, root=1, cookie=cookie, timeout=timeout), candidate
        except ResolveError as failed:
            failure = failed
    raise failure or ResolveError("TeraBox refused the share listing")


async def _walk(
    session: requests.AsyncSession, token: str, surl: str, cookie: str | None = None, *, timeout: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every file in the share, following folders."""
    root, working = await _root_listing(session, token, surl, cookie=cookie, timeout=timeout)
    files = [item for item in root.get("list") or [] if not _is_dir(item)]
    pending = [item.get("path") or "" for item in root.get("list") or [] if _is_dir(item)]
    seen: set[str] = set()

    while pending:
        folder = pending.pop(0)
        if not folder or folder in seen:
            continue
        seen.add(folder)
        data = await _list(session, token, working, root=0, folder=folder, cookie=cookie, timeout=timeout)
        for item in data.get("list") or []:
            if _is_dir(item):
                pending.append(item.get("path") or "")
            else:
                files.append(item)

    return files, root


def _direct(url: str, items: list[dict[str, Any]], cookie: str | None) -> Resolution:
    """Method A: the listing carried signed links, so these need no account.

    Kept because the API still *can* answer with a `dlink` — it did not for the shares measured,
    which is why Method B exists — and when it does, this is far cheaper than copying into an
    account.
    """
    files: list[ResolvedFile] = []
    taken: set[str] = set()
    for item in items:
        size = int(item.get("size") or 0)
        name = unique_name(safe_filename(str(item.get("server_filename") or "terabox")), taken)
        taken.add(name)
        files.append(_resolved(name, size, str(item["dlink"]), cookie, url))
    return Resolution(engine="terabox", source_url=url, files=tuple(files))


def _cookie_string(cookies: str | None, cookies_file: Path | None) -> str | None:
    """A `Cookie:` header value, from the caller's string or their legacy cookie file.

    A bare value is wrapped as `ndus=…`, because that is what `/setting` collects: V4's own prompt
    read ``ndus=YOUR_VALUE; browserid=...``, so the distinction does not survive into storage and
    only the wrapped form is accepted by the API.
    """
    raw = (cookies or "").strip()
    if not raw and cookies_file is not None and cookies_file.exists():
        try:
            raw = cookies_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        found = re.search(r"ndus=([^;\s\"']+)", raw)
        if found:
            raw = f"ndus={found.group(1)}"
    if not raw:
        return None
    return raw if "=" in raw else f"ndus={raw}"


#: V4's landing folder: Method B copies a share into the account before a link can be issued for it.
_LANDING_DIR = "/downloads"

#: How long a copy may sit in the account before the next resolve clears it out.
#:
#: V4 never deleted them, so the account filled up with every file it had ever served. Deletion
#: cannot happen when the link is issued — the copy has to exist while it is being fetched — so it
#: happens on the *next* use of that account, after a grace period long enough that a download still
#: running (or a second job reusing the same copy) is never cut off mid-file.
_LANDING_TTL_SECONDS = 6 * 3600

#: V4: the account API answers this while a video is still being transcoded, and it retried.
_TRANSCODING_ERRNO = 130
_TRANSCODE_ATTEMPTS = 10
_TRANSCODE_WAIT_SECONDS = 5.0

#: TeraBox's verification gate, seen on both routes that issue a link (`/share/transfer` 400810,
#: `/share/download` 400310). Server-side policy, so porting around it is not possible.
_GATE_ERRNOS = frozenset({400810, 400310})
_GATE_MESSAGE = (
    "TeraBox will not issue a download link for this share until the account completes its "
    "verification (`need verify_v2`). That is TeraBox's own gate, not this bot's: open the share "
    "in a browser, complete the verification, then set a fresh cookie with /setting"
)


def _account_headers(cookie: str) -> dict[str, str]:
    """The account API is called from the account's own pages, so it wants that identity."""
    return {
        "User-Agent": bridge.USER_AGENT,
        "Cookie": cookie,
        "Referer": f"https://{_API_HOST}/main?category=all&path=%2F",
    }


def _account_params(token: str, bdstoken: str) -> dict[str, str]:
    """V4's `_query_params()` for the account API — `jsToken` only when one was scraped."""
    params = {
        "app_id": "250528",
        "web": "1",
        "channel": "dubox",
        "clienttype": "0",
        "bdstoken": bdstoken,
    }
    if token:
        params["jsToken"] = token
    return params


def _gate_reason(errno: Any) -> str | None:
    """The user-facing reason for an account call TeraBox refused, when it is one we recognise."""
    return _GATE_MESSAGE if errno in _GATE_ERRNOS else None


def _resolved(
    name: str,
    size: int,
    link: str,
    cookie: str | None,
    source_url: str,
    *,
    role: TransferRole = "file",
) -> ResolvedFile:
    """One file, with the headers its link needs attached.

    A TeraBox link is issued to a session, so the fetcher must present the same cookie and a
    matching `Referer`; without that a link the resolver already obtained answers 403.
    """
    headers = {"Referer": f"https://{_API_HOST}/main?category=all&path=%2F"}
    if cookie:
        headers["Cookie"] = cookie
    return ResolvedFile(
        transfers=(Transfer(url=link, role=role, filesize=size or None, headers=headers),),
        filename=name,
        media_kind=kind_for(name.rsplit(".", 1)[-1]),
        filesize=size or None,
        source_url=source_url,
    )


async def _bdstoken(session: requests.AsyncSession, cookie: str, *, timeout: float) -> str | None:
    """The account token, without which every account call answers `errno -6`.

    The page is URL-encoded: V4's `_resolve_tokens()` unquoted it first, and regexing the raw text —
    which an earlier version of this adapter did — reports a live cookie as an expired one.
    """
    try:
        response = await session.get(
            f"https://{_API_HOST}/main",
            headers={"User-Agent": bridge.USER_AGENT, "Cookie": cookie},
            timeout=timeout,
        )
        match = _BDSTOKEN_RE.search(urllib.parse.unquote(response.text))
        return match.group(1) if match else None
    except Exception as failed:
        log.debug("TeraBox bdstoken scrape failed: %s", failed)
        return None


async def _ensure_landing(
    session: requests.AsyncSession, cookie: str, bdstoken: str, *, timeout: float
) -> None:
    """Create the landing folder, as V4 did before transferring. Its answer carries nothing."""
    try:
        await session.post(
            f"https://{_API_HOST}/api/create",
            params=_account_params("", bdstoken),
            data={"path": _LANDING_DIR, "isdir": "1", "block_list": "[]"},
            headers=_account_headers(cookie),
            timeout=timeout,
        )
    except Exception as failed:
        log.debug("TeraBox landing folder could not be created: %s", failed)


async def _landing_entries(
    session: requests.AsyncSession, cookie: str, bdstoken: str, *, timeout: float
) -> list[dict[str, Any]]:
    """`/api/list` on the landing folder, as it arrives, or `[]` when it cannot be read.

    One request serves two purposes: noticing a file already in the account (a retry, or a share a
    previous job copied) and finding copies old enough to remove.
    """
    try:
        response = await session.get(
            f"https://{_API_HOST}/api/list",
            params={
                **_account_params("", bdstoken),
                "dir": _LANDING_DIR,
                "order": "time",
                "desc": "1",
                "num": "1000",
            },
            headers=_account_headers(cookie),
            timeout=timeout,
        )
        data = response.json()
    except Exception as failed:
        log.debug("TeraBox account listing failed: %s", failed)
        return []

    return list(data.get("list") or [])


def _account_filenames(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Those entries as ``{filename: fs_id}`` — V4's `_get_existing_files()`."""
    return {
        str(entry.get("server_filename")): str(entry.get("fs_id"))
        for entry in entries
        if entry.get("server_filename") and entry.get("fs_id")
    }


async def _delete_copy(
    session: requests.AsyncSession,
    cookie: str,
    bdstoken: str,
    fs_id: str,
    *,
    timeout: float,
) -> bool:
    """`/api/filemanager?opera=delete` — measured, because `/api/delete` answers 405.

    Best-effort by design: it reclaims space, so a refusal is worth a log line and nothing more. The
    refusal this account actually gives today is `errno 450016 need verify` — the same verification
    gate as the transfer, which is also why nothing is being copied in the first place.
    """
    try:
        response = await session.post(
            f"https://{_API_HOST}/api/filemanager",
            params={**_account_params("", bdstoken), "opera": "delete"},
            data={"filelist": json.dumps([str(fs_id)])},
            headers=_account_headers(cookie),
            timeout=timeout,
        )
        data = response.json()
    except Exception as failed:
        log.debug("TeraBox could not delete copy %s: %s", fs_id, failed)
        return False

    if data.get("errno") != 0:
        log.debug("TeraBox refused to delete copy %s: %s", fs_id, data.get("errmsg"))
        return False
    return True


async def _sweep_landing(
    session: requests.AsyncSession,
    cookie: str,
    bdstoken: str,
    entries: list[dict[str, Any]],
    *,
    keep: set[str],
    timeout: float,
) -> int:
    """Remove copies the account has finished with; return how many went.

    A copy is finished with when it is older than `_LANDING_TTL_SECONDS` *and* is not one this
    resolve is about to fetch. Folders are left alone, and so is anything without a date to judge.
    """
    now = time.time()
    removed = 0
    for entry in entries:
        name = str(entry.get("server_filename") or "")
        fs_id = entry.get("fs_id")
        if not name or not fs_id or name in keep:
            continue
        if int(entry.get("isdir") or 0) == 1:
            continue
        try:
            created = float(entry.get("server_mtime") or 0)
        except (TypeError, ValueError):
            continue
        if not created or now - created < _LANDING_TTL_SECONDS:
            continue
        if await _delete_copy(session, cookie, bdstoken, str(fs_id), timeout=timeout):
            removed += 1
    return removed


async def _copy_into_account(
    session: requests.AsyncSession,
    cookie: str,
    bdstoken: str,
    *,
    share_id: str,
    uk: str,
    item: dict[str, Any],
    timeout: float,
) -> str:
    """`/share/transfer` — copy one file into the account and return the id to ask for its link.

    Faithful to V4: the copy's *new* id is taken from the response's `extra.list[0].to_fs_id` when
    present, because otherwise the copy has to be found again by name. `errno` 12 and -33 mean
    "it is already there", which is a success for this purpose — the caller looks it up by name.
    """
    response = await session.post(
        f"https://{_API_HOST}/share/transfer",
        params={**_account_params("", bdstoken), "shareid": share_id, "from": uk},
        data={"fsidlist": f"[{item.get('fs_id')}]", "path": _LANDING_DIR},
        headers=_account_headers(cookie),
        timeout=timeout,
    )
    data = response.json()
    errno = data.get("errno")
    if errno in (12, -33):
        return ""
    if errno != 0:
        raise ResolveError(
            _gate_reason(errno) or f"TeraBox refused to copy the file (errno {errno})"
        )

    extra = (data.get("extra") or {}).get("list") or []
    if extra and extra[0].get("to_fs_id"):
        return str(extra[0]["to_fs_id"])
    return ""


async def _download_link(
    session: requests.AsyncSession,
    cookie: str,
    bdstoken: str,
    token: str,
    fs_id: str,
    *,
    timeout: float,
) -> str | None:
    """`/api/filemetas` — the link to a file that is *in the account*, which is the whole point of
    copying it first."""
    try:
        response = await session.get(
            f"https://{_API_HOST}/api/filemetas",
            params={
                **_account_params(token, bdstoken),
                "fsids": json.dumps([str(fs_id)]),
                "dlink": "1",
                "thumb": "0",
            },
            headers=_account_headers(cookie),
            timeout=timeout,
        )
        data = response.json()
    except Exception as failed:
        log.debug("TeraBox filemetas failed: %s", failed)
        return None

    gate = _gate_reason(data.get("errno"))
    if gate:
        raise ResolveError(gate)
    for entry in data.get("list") or data.get("info") or []:
        if entry.get("dlink"):
            return str(entry["dlink"])
    return None


async def _stream_url(
    session: requests.AsyncSession,
    cookie: str,
    bdstoken: str,
    path: str,
    *,
    timeout: float,
) -> str | None:
    """`/api/streaming` — V4's `stream` mode, used here when a file has no plain link.

    V4 retried while TeraBox answered `errno 130` (still transcoding), which is the difference
    between "this video has no link yet" and "ask again in five seconds". A manifest is returned as
    an `hls` transfer so the fetcher consumes it with ffmpeg rather than writing a playlist to disk.
    """
    params = {**_account_params("", bdstoken), "path": path, "type": "M3U8_AUTO_480"}
    for _ in range(_TRANSCODE_ATTEMPTS):
        try:
            response = await session.get(
                f"https://{_API_HOST}/api/streaming",
                params=params,
                headers=_account_headers(cookie),
                timeout=timeout,
            )
            body = response.text
            if "#EXTM3U" in body:
                return str(response.url)
            data = response.json()
        except Exception as failed:
            log.debug("TeraBox streaming failed: %s", failed)
            return None

        errno = data.get("errno")
        if errno == _TRANSCODING_ERRNO:
            await asyncio.sleep(_TRANSCODE_WAIT_SECONDS)
            continue
        if errno == 0:
            return str(data.get("lurl") or data.get("m3u8_url") or "") or None
        return None
    return None


async def _resolve_via_account(
    token: str,
    cookie: str,
    listing: list[dict[str, Any]],
    *,
    share_id: str,
    uk: str,
    source_url: str,
    timeout: float,
) -> list[ResolvedFile]:
    """V4's Method B: copy each file into the account, then resolve the link to the copy.

    The order is V4's — validate the session, ensure the landing folder, read what is already there,
    then per file transfer and ask for the link, preferring the copy's id from the transfer
    response. What is deliberately *not* V4's is the failure handling: a refusal V4 recorded per
    file and mostly swallowed becomes the reason the user is given, because TeraBox's `need
    verify_v2` gate is the only explanation that matters when nothing resolves.
    """
    session = bridge.session_for(timeout)
    try:
        bdstoken = await _bdstoken(session, cookie, timeout=timeout)
        if not bdstoken:
            raise ResolveError(
                "TeraBox rejected the stored cookie (no account token came back): it has probably "
                "expired — set a fresh one with /setting"
            )

        await _ensure_landing(session, cookie, bdstoken, timeout=timeout)
        entries = await _landing_entries(session, cookie, bdstoken, timeout=timeout)
        existing = _account_filenames(entries)

        # Clear out what earlier resolves left behind, keeping the names this one is about to
        # fetch — and doing it before the transfers, so the space is free when it is needed.
        removed = await _sweep_landing(
            session,
            cookie,
            bdstoken,
            entries,
            keep={str(item.get("server_filename") or "") for item in listing},
            timeout=timeout,
        )
        if removed:
            log.info("TeraBox account: removed %d old copy/copies", removed)

        resolved: list[ResolvedFile] = []
        reason: str | None = None
        for item in listing:
            name = safe_filename(str(item.get("server_filename") or "terabox"))
            size = int(item.get("size") or 0)
            raw_name = str(item.get("server_filename") or "")
            try:
                fs_id = existing.get(raw_name) or await _copy_into_account(
                    session,
                    cookie,
                    bdstoken,
                    share_id=share_id,
                    uk=uk,
                    item=item,
                    timeout=timeout,
                )
                link = await _download_link(
                    session, cookie, bdstoken, token, fs_id or str(item.get("fs_id") or ""),
                    timeout=timeout,
                )
                if link is None:
                    link = await _stream_url(
                        session, cookie, bdstoken, str(item.get("path") or ""), timeout=timeout
                    )
                    if link:
                        resolved.append(
                            _resolved(name, size, link, cookie, source_url, role="hls")
                        )
                        continue
                    reason = reason or "TeraBox returned no download link for this file"
                    continue
                resolved.append(_resolved(name, size, link, cookie, source_url))
            except ResolveError as failed:
                reason = reason or str(failed)

        if not resolved:
            raise ResolveError(reason or "TeraBox returned no download links for this share")
        return resolved
    finally:
        await bridge.close(session)


def resolver(
    cookies_file: Path | None = None,
    options: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resolver:
    """Factory for the registry.

    The cookie arrives as a *string* in the adapter's own options slice (`{"cookies": "ndus=…"}`)
    rather than as a file, because that is what `/setting` collects and what the share API wants;
    `cookies_file` stays for compatibility and for whoever already has a jar on disk.
    """
    async def _attempt(url: str) -> Resolution:
        return await attempt(
            url, cookies_file=cookies_file, options=options, timeout=timeout
        )
    return Resolver(name="terabox", attempt=_attempt)