"""Parsing ``t.me`` links into something a client can act on (FR-4).

Four forms reach ``/dt``, and they are one grammar with three modifiers:

* a **message link** — ``t.me/<username>/<id>`` (a channel post or a public chat) or
  ``t.me/c/<internal>/<id>`` (a private chat, addressed by its own id);
* a **topic link** — ``t.me/<username>/<topic>/<id>``, because a forum chat puts the
  topic id in the path where the message id usually is;
* an **album** — not a link form at all: the message link is the album's cover, and the
  grouping is discovered by fetching around it (see :mod:`telegram.media`);
* a **range** — two message links joined by a dash, which is the convention the previous
  implementation used and the only way to express "these messages" in a URL.

A **folder link** (``t.me/addlist/<slug>``) is the fourth form FR-4.3 asks for and the only
one that is not a link to content: it names a chat filter, and what it points at has to be
resolved through the user's own session. It parses into :class:`FolderLink` and nothing more
— enumerating a filter is a client call, not a parsing decision.

Everything here is pure: no client, no network, no settings. That is deliberate, because a
malformed link must produce FR-4.6's *specific parse error* rather than an exception from
somewhere deep in the fetch path, and the cheapest way to guarantee the message names the
actual problem is to make parsing a function that only returns or raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The hosts Telegram serves links from. `telegram.dog` is the old short domain and is
#: still in circulation in older chats, which is exactly the sort of thing a user pastes.
HOSTS = ("t.me", "telegram.me", "telegram.dog")

#: Separates the two ends of a range. An en dash is accepted because it is what a phone
#: keyboard produces and, in a range of ids, it never means anything else.
_RANGE_SEPARATORS = ("-", "–")

_PRIVATE = "c"
_PREVIEW = "s"
_FOLDER = "addlist"

_HOST_ALTERNATION = "|".join(host.replace(".", r"\.") for host in HOSTS)
_LINK = rf"(?:https?://)?(?:{_HOST_ALTERNATION})"
_MESSAGE_RE = re.compile(
    rf"^{_LINK}/(?:(?P<private>{_PRIVATE})/(?P<internal>\d+)"
    rf"|(?P<folder>{_FOLDER})/(?P<slug>[A-Za-z0-9_-]+)"
    rf"|(?:{_PREVIEW}/)?(?P<username>[A-Za-z0-9_]+))"
    rf"(?:/(?P<topic>\d+))?/(?P<message>\d+)"
    # A link copied out of a client can carry a query (`?comment=12&single`) or a
    # fragment. They say where the link was opened from, not what it points at.
    r"(?:[?#].*)?/?$",
    re.IGNORECASE,
)
_FOLDER_ONLY_RE = re.compile(
    rf"^{_LINK}/{_FOLDER}/(?P<slug>[A-Za-z0-9_-]+)(?:[?#].*)?/?$",
    re.IGNORECASE,
)


class TgLinkError(ValueError):
    """A link that is not one of the accepted forms (FR-4.6).

    The message is user-facing, so it says what was wrong with *this* link rather than
    listing the grammar.
    """


@dataclass(frozen=True)
class MessageLink:
    """One message in one chat."""

    #: A username, or the internal id for a private chat. Kept as the user's link wrote it
    #: so the error copy can quote it back, and resolved to an id only at the client call.
    chat: str
    message_id: int
    #: Present for a forum chat (``t.me/<chat>/<topic>/<id>``). The client needs it to fetch
    #: the message, because a topic is its own message thread.
    topic_id: int | None = None
    #: True for the ``c/<internal>`` form, where ``chat`` is a numeric id rather than a name.
    private: bool = False

    @property
    def chat_id(self) -> int | str:
        """What to hand the client.

        Telegram addresses a channel by ``-100`` followed by its internal id, and that
        prefix is what distinguishes a channel from a user in the API's own id space — so
        ``c/1234567890/5`` addresses channel ``-1001234567890``. A username is passed
        through, because the client resolves it.
        """
        return int(f"-100{self.chat}") if self.private else self.chat


@dataclass(frozen=True)
class RangeLink:
    """Two message links, inclusive at both ends and ordered by id."""

    start: MessageLink
    end: MessageLink

    @property
    def count(self) -> int:
        """How many ids the range spans. Includes messages without media, deliberately:
        FR-4.5 reports *messages scanned*, which is this number, alongside files."""
        return self.end.message_id - self.start.message_id + 1

    def ids(self) -> list[int]:
        return list(range(self.start.message_id, self.end.message_id + 1))


@dataclass(frozen=True)
class FolderLink:
    """A shared chat folder — ``t.me/addlist/<slug>`` — which names a filter, not content."""

    slug: str


TgTarget = MessageLink | RangeLink | FolderLink


def _message(match: re.Match[str]) -> MessageLink:
    internal = match.group("internal")
    username = match.group("username")
    if internal:
        chat, private = internal, True
    elif username:
        chat, private = username, False
    else:  # pragma: no cover - the pattern has no other way to match a message
        raise TgLinkError("That link does not name a chat.")

    topic = match.group("topic")
    return MessageLink(
        chat=chat,
        message_id=int(match.group("message")),
        topic_id=int(topic) if topic else None,
        private=private,
    )


def parse(text: str) -> TgTarget:
    """The target a link names, or :class:`TgLinkError` saying why it is not one.

    A range is tried first: ``t.me/a/1-t.me/a/2`` looks like a message link with a long
    tail, so a single-link match would silently download one message and drop the other
    end — the sort of quiet wrong answer that is worse than a refusal.
    """
    candidate = (text or "").strip()
    if not candidate:
        raise TgLinkError("No link was given. Send a link to a Telegram message.")

    folder = _FOLDER_ONLY_RE.match(candidate)
    if folder:
        return FolderLink(slug=folder.group("slug"))

    for separator in _RANGE_SEPARATORS:
        if separator not in candidate:
            continue
        left, _, right = candidate.partition(separator)
        left, right = left.strip(), right.strip()
        # Only two *links* make a range: "t.me/a/1-2" is not one, and neither is prose that
        # happens to contain a dash.
        if not _looks_like_link(left) or not _looks_like_link(right):
            continue
        start, end = parse_one(left), parse_one(right)
        if start.chat != end.chat or start.topic_id != end.topic_id:
            raise TgLinkError(
                "A range must stay in one chat: "
                f"`{start.chat}` and `{end.chat}` are different."
            )
        low, high = sorted((start, end), key=lambda link: link.message_id)
        return RangeLink(start=low, end=high)

    return parse_one(candidate)


def parse_one(text: str) -> MessageLink:
    """One message link. Folders are not messages, so this rejects them (FR-4.6)."""
    candidate = text.strip().rstrip(")")
    match = _MESSAGE_RE.match(candidate)
    if not match:
        raise TgLinkError(
            f"`{candidate}` is not a Telegram message link.\n"
            "Send one like `https://t.me/channel/123` or `/dl` for any other site."
        )
    if match.group("folder"):
        raise TgLinkError(
            "A folder link names a whole folder, not a message. "
            "Send a message link inside it, or a range."
        )
    return _message(match)


def _looks_like_link(text: str) -> bool:
    """Whether this half of a dash-split could be a message link at all.

    Used to decide *range or not* before reporting an error, so a plain typo in a single
    link is not reported as a malformed range.
    """
    lowered = text.lower()
    return any(host in lowered for host in HOSTS)
