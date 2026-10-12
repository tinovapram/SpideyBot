"""Shared plumbing for the web-bridge adapters (D6).

Most of the native adapters are a thin wrapper around one third-party endpoint: a "saver" site that
takes a link and answers with the direct media URLs, because the host itself refuses to. The
site-specific part of each is the *endpoint* and the *shape of the answer*; the rest — build a
browser-like session, convert a transport failure into "try the next resolver", dig a URL out of an
unpredictable JSON blob, name the file so two same-titled items do not overwrite each other — is
identical in twenty files, which would be twenty places for the same bug.

So this module owns the around-it, and each adapter owns its site. Two consequences worth stating:

* **A transport failure becomes ``ResolveError``.** The chain's rule is "if this resolver cannot do
  it, try the next one" (:func:`downloader.flow.resolve_through_chain`), and that only works if a
  dead bridge, a 500 or a timeout is reported as *this resolver's* failure rather than as a crash.
  A bridge site dying is the normal condition of bridge sites.
* **The session is injectable.** Thirteen of the sites this package covers are third-party saver
  sites, so a test that needs the network cannot run at all. ``client=`` lets a test hand in a
  session that returns a recorded answer, which is what makes the payload the contract under test.
"""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
import urllib.parse
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi import requests

from downloader.contracts import (
    Resolution,
    ResolvedFile,
    ResolveError,
    Transfer,
    kind_for,
    safe_filename,
    unique_name,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0

#: What a browser sends. The saver sites are Cloudflare-fronted often enough that this is the
#: difference between an answer and a 403, and `curl_cffi`'s impersonation covers the rest.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Extensions a "no usable URL in the answer" hunt accepts, in the order a bridge usually names
#: them: a video post must not resolve to its thumbnail just because the thumbnail came first.
MEDIA_HINTS = (".mp4", ".m4v", ".webm", ".m3u8", ".jpg", ".jpeg", ".png", ".gif", ".mp3", ".m4a")


def host_matches(url: str, suffixes: Sequence[str]) -> bool:
    """Whether ``url``'s host is one of ``suffixes``, with subdomains allowed and lookalikes not.

    ``endswith("." + suffix)`` rather than ``in``: ``mega.nz.evil.test`` must not match ``mega.nz``,
    which is the same trap the registry's video-host rule already guards against.
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def session_for(
    timeout: float = DEFAULT_TIMEOUT,
    *,
    referer: str | None = None,
    headers: dict[str, str] | None = None,
) -> requests.AsyncSession:
    """A browser-like session, impersonating Chrome because the bridges check for one."""
    client = requests.AsyncSession(impersonate="chrome120", timeout=timeout)
    client.headers.update({"User-Agent": USER_AGENT})
    if referer:
        client.headers.update({"Referer": referer})
    if headers:
        client.headers.update(headers)
    return client


async def close(client: Any) -> None:
    """Close a session, tolerating a fake that has nothing to close."""
    closer = getattr(client, "close", None)
    if closer is None:
        return
    result = closer()
    if hasattr(result, "__await__"):
        await result


async def load_cookies(
    client: Any,
    cookies_file: Path | None,
    *,
    default_domain: str,
) -> None:
    """Put a user's cookies into the session (D8, FR-14).

    Two formats are accepted, deliberately, because the two halves of this project disagree about
    which one a cookie file is: the engines are given ``--cookies`` (Netscape, the format
    ``cookies.txt`` browser extensions export), while the previous implementation's adapters read a
    JSON list. Accepting both means the file P7's ``/setting`` uploads works here whichever it turns
    out to be, and a wrong guess is then impossible rather than silent — the failure mode of a
    format mismatch is *no cookies sent*, which looks exactly like an expired login.
    """
    if cookies_file is None or not cookies_file.is_file():
        return

    try:
        raw = cookies_file.read_text(encoding="utf-8")
    except OSError as error:
        log.warning("could not read the cookie file: %s", error)
        return

    cookies = _netscape_cookies(raw)
    if not cookies:
        cookies = _json_cookies(raw)

    if not cookies:
        log.warning("the cookie file held no cookies in either format")
        return

    for domain, name, value in cookies:
        try:
            client.cookies.set(name, value, domain=domain or default_domain)
        except Exception:  # noqa: BLE001 - one bad cookie must not lose the rest
            log.warning("ignoring cookie %r: the jar refused it", name)


def _netscape_cookies(raw: str) -> list[tuple[str, str, str]]:
    """``cookies.txt``: tab-separated ``domain, flag, path, secure, expires, name, value``."""
    found: list[tuple[str, str, str]] = []
    for line in raw.splitlines():
        # ``#HttpOnly_`` is the one comment prefix that carries a real cookie.
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7:
            found.append((fields[0], fields[5], fields[6]))
    return found


def _json_cookies(raw: str) -> list[tuple[str, str, str]]:
    """The previous implementation's shape: a list of ``{name, value, domain}`` objects."""
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    found: list[tuple[str, str, str]] = []
    for item in data:
        if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
            found.append((str(item.get("domain") or ""), str(item["name"]), str(item["value"])))
    return found


async def get_text(
    client: Any,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> str:
    """GET a page or an API and return its body, as ``ResolveError`` on any transport failure."""
    return await _text(client, "GET", url, headers=headers, params=params, timeout=timeout)


async def post_text(
    client: Any,
    url: str,
    *,
    data: Any = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> str:
    """POST to a bridge and return its body. JSON or form-encoded, whichever the site wants."""
    return await _text(
        client, "POST", url, headers=headers, data=data, json_body=json_body, timeout=timeout
    )


async def _text(
    client: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    data: Any = None,
    json_body: Any = None,
    timeout: float | None = None,
) -> str:
    kwargs: dict[str, Any] = {"headers": headers or {}}
    if params is not None:
        kwargs["params"] = params
    if data is not None:
        kwargs["data"] = data
    if json_body is not None:
        kwargs["json"] = json_body
    if timeout is not None:
        kwargs["timeout"] = timeout

    try:
        response = await getattr(client, method.lower())(url, **kwargs)
    except Exception as error:  # noqa: BLE001 - the transport's message is the useful one
        raise ResolveError(f"could not reach the extractor service: {error}") from error

    status = getattr(response, "status_code", 200)
    if status >= 400:
        raise ResolveError(f"the extractor service answered {status}")
    return response.text


async def get_final_url(
    client: Any,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> str:
    """GET a URL and return where it ended up, for a share link that is only a redirect.

    The body is thrown away: the caller wants the destination — an id lives in its path or query,
    and following the redirect is the only way to learn it. Reported as ``ResolveError`` like every
    other transport failure, because "this resolver could not do it" is the chain's contract.
    """
    kwargs: dict[str, Any] = {"headers": headers or {}, "allow_redirects": True}
    if timeout is not None:
        kwargs["timeout"] = timeout

    try:
        response = await client.get(url, **kwargs)
    except Exception as error:  # noqa: BLE001 - the transport's message is the useful one
        raise ResolveError(f"could not follow the share link: {error}") from error
    return str(getattr(response, "url", url))


async def get_json(client: Any, url: str, **kwargs: Any) -> Any:
    """GET an endpoint that answers with JSON."""
    return _as_json(await get_text(client, url, **kwargs), url)


async def post_json(client: Any, url: str, **kwargs: Any) -> Any:
    """POST to an endpoint that answers with JSON."""
    return _as_json(await post_text(client, url, **kwargs), url)


def _as_json(body: str, url: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as error:
        raise ResolveError(f"the extractor service did not answer with JSON: {error}") from error


def soup_of(html: str) -> BeautifulSoup:
    """Parse a bridge's HTML answer. Several of them reply with a rendered fragment, not data."""
    return BeautifulSoup(html, "html.parser")


def find_url(obj: Any, *, patterns: Iterable[str] = MEDIA_HINTS) -> str | None:
    """The first URL in a JSON-like structure that looks like media.

    For the bridges whose answer shape is undocumented and changes: hunting for a ``.mp4`` is more
    robust than naming a key that the site is free to rename, and a miss is a clean ``ResolveError``
    rather than a wrong file.
    """
    wanted = tuple(patterns)
    if isinstance(obj, dict):
        # Keys first, so a "download_url" beats an unrelated CDN link found deeper in the blob.
        for key in ("download_url", "downloadUrl", "url", "video_url", "video", "link", "src"):
            value = obj.get(key)
            if isinstance(value, str) and _looks_like_media(value, wanted):
                return value
        for value in obj.values():
            found = find_url(value, patterns=wanted)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_url(value, patterns=wanted)
            if found:
                return found
    elif isinstance(obj, str) and _looks_like_media(obj, wanted):
        return obj
    return None


def _looks_like_media(value: str, patterns: Sequence[str]) -> bool:
    if not value.startswith(("http://", "https://")):
        return False
    return any(pattern in value.lower() for pattern in patterns)


def filename_for(
    title: str,
    *,
    extension: str,
    index: int | None = None,
    total: int | None = None,
    taken: Sequence[str] = (),
) -> str:
    """A safe filename for one item of a post, numbered only when it is not the only one.

    ``taken`` is what makes a share with two identically-titled items deliver two files instead of
    one (D37's rule, already implemented in :func:`downloader.contracts.unique_name`).
    """
    stem = safe_filename(title)
    suffix = f"_{index}" if index is not None and (total or 0) > 1 else ""
    return unique_name(f"{stem}{suffix}{extension}", taken)


def media(
    url: str,
    filename: str,
    *,
    role: str = "file",
    filesize: int | None = None,
    source_url: str | None = None,
    note: str | None = None,
) -> ResolvedFile:
    """One transfer that produces one deliverable, classified by the name it will land under.

    The kind comes from the *filename* rather than from the URL, because that is what delivery
    dispatches on (FR-8.1) — and because an HLS transfer's URL is a playlist while its output is an
    ``.mp4``, so classifying by URL would send a video as a document.
    """
    kind = kind_for(Path(filename).suffix)
    return ResolvedFile(
        transfers=(Transfer(url=url, role=role, filesize=filesize),),  # type: ignore[arg-type]
        filename=filename,
        media_kind=kind,
        filesize=filesize,
        source_url=source_url,
        note=note,
    )


def resolution(engine: str, url: str, files: Sequence[ResolvedFile]) -> Resolution:
    """A resolution, or the E4-shaped "the post held nothing" error when there are no files."""
    if not files:
        raise ResolveError("no downloadable media found in that post", empty=True)
    return Resolution(engine=engine, source_url=url, files=tuple(files))


@contextlib.asynccontextmanager
async def opened(
    client: Any | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    referer: str | None = None,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[Any]:
    """A session for one attempt: the caller's if injected, otherwise a fresh one that is closed.

    Injected-first is what makes an offline test possible (the site is a third party, so the network
    cannot be part of the assertion), and closing only what we opened is what keeps one worker's
    connection pool from being shut down by somebody else's resolve.
    """
    if client is not None:
        yield client
        return

    created = session_for(timeout, referer=referer, headers=headers)
    try:
        yield created
    finally:
        await close(created)


async def json_bridge(
    *,
    engine: str,
    source_url: str,
    endpoint: str,
    default_title: str,
    payload: dict[str, Any] | None = None,
    method: str = "POST",
    form: bool = False,
    headers: dict[str, str] | None = None,
    referer: str | None = None,
    extension: str = ".mp4",
    cookies_file: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> Resolution:
    """Resolve a link through one of the "saver" sites that answers with JSON (D6).

    Nine of the adapters are this shape: hand the link to somebody else's endpoint, and take the
    first media URL out of whatever it answers with. Written once because the only real difference
    is which key the URL hides under, and because the *default* title and extension have to agree
    with what the answer actually is — a bridge that answered with an image and a ``.mp4`` name
    would deliver a broken video (FR-8.1). A file is classified from its name, which is why the
    extension decides the Telegram method.
    """
    async with opened(client, timeout=timeout, referer=referer, headers=headers) as session:
        await load_cookies(session, cookies_file, default_domain=endpoint)

        request: dict[str, Any] = {"headers": {**(headers or {}), "Accept": "application/json"}}
        if method == "GET":
            answer = await get_json(session, endpoint, **request)
        elif form:
            answer = await post_json(session, endpoint, data=payload or {}, **request)
        else:
            answer = await post_json(session, endpoint, json_body=payload or {}, **request)

    media_url = find_url(answer)
    if not media_url:
        raise ResolveError(f"{engine} did not return a downloadable URL")

    extension = _extension_of(media_url, fallback=extension)
    title = _title_of(answer) or default_title
    filename = filename_for(title, extension=extension)
    # The role says what the transfer *is*, which is not the same question as what Telegram should
    # send: a single audio transfer is still audio, and naming it `file` would lose that. Delivery
    # reads the file's kind from its name, so this does not change what the user receives — it keeps
    # the contract honest for the one consumer that does care, the merge path.
    role = "audio" if kind_for(extension) == "audio" else "file"
    return resolution(
        engine, source_url, [media(media_url, filename, role=role, source_url=source_url)]
    )


def _title_of(answer: Any) -> str | None:
    """The title a bridge reports, whatever key it chose, when it is a usable string."""
    if not isinstance(answer, dict):
        return None
    for key in ("title", "name", "filename", "video_title"):
        value = answer.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Some bridges nest it: `{"data": {"title": ...}}`.
    nested = answer.get("data") or answer.get("result")
    if isinstance(nested, dict):
        return _title_of(nested)
    return None


def _extension_of(url: str, *, fallback: str) -> str:
    """The extension the URL itself claims, because that is what the file will be.

    Preferred over a per-adapter constant: a bridge answering with a ``.jpg`` for an image post must
    not be named ``.mp4``, or delivery would send a photo as a video and the user would get a file
    that does not play (FR-8.1).
    """
    suffix = pathlib.PurePosixPath(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in MEDIA_HINTS:
        # HLS is delivered as an MP4: the playlist is consumed by ffmpeg, and `.m3u8` is not a
        # container a Telegram client can play, which is the whole point of the `hls` role.
        return ".mp4" if suffix == ".m3u8" else suffix
    return fallback
