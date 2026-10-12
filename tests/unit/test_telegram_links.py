"""``/dt`` link parsing (FR-4.3, FR-4.6).

The parser is the only thing between a user's paste and a client call, so the tests are about the
*shape* of the answer: which chat, which message, and which of the four accepted forms. The error
cases matter as much as the happy ones, because FR-4.6 asks for a specific parse error and the
cheapest way to get that wrong is to let a bad link travel further than the parser.
"""

from __future__ import annotations

import pytest

from telegram import links

# ── the accepted forms ──────────────────────────────────────────────────


def test_a_public_message_link_names_its_chat_and_message() -> None:
    parsed = links.parse("https://t.me/durov/123")

    assert isinstance(parsed, links.MessageLink)
    assert parsed.chat == "durov"
    assert parsed.message_id == 123
    assert not parsed.private
    assert parsed.chat_id == "durov", "a username is resolved by the client, not here"


def test_a_private_link_becomes_a_channel_id() -> None:
    """``c/<internal>`` addresses a channel, and Telegram spells that ``-100<internal>``."""
    parsed = links.parse("t.me/c/1234567890/7")

    assert isinstance(parsed, links.MessageLink)
    assert parsed.private
    assert parsed.chat_id == -1001234567890


def test_the_web_preview_form_is_the_same_message() -> None:
    """``/s/`` is what a browser shows; the message behind it is identical."""
    assert links.parse("https://t.me/s/telegram/42") == links.parse("https://t.me/telegram/42")


def test_a_forum_topic_keeps_its_topic_id() -> None:
    parsed = links.parse("https://t.me/chat/9/77")

    assert isinstance(parsed, links.MessageLink)
    assert parsed.topic_id == 9
    assert parsed.message_id == 77


@pytest.mark.parametrize("tail", ["?single", "?comment=12", "#top"])
def test_a_query_or_fragment_does_not_change_the_message(tail: str) -> None:
    """Links copied out of a client carry these; they say where it was opened from."""
    parsed = links.parse(f"https://t.me/durov/123{tail}")

    assert isinstance(parsed, links.MessageLink)
    assert parsed.message_id == 123


def test_the_old_short_domain_still_parses() -> None:
    assert links.parse("https://telegram.dog/durov/5") == links.MessageLink("durov", 5)


def test_a_folder_link_parses_as_a_folder() -> None:
    """FR-4.3's fourth form. A folder is the only link that names no content."""
    parsed = links.parse("https://t.me/addlist/abc-def")

    assert parsed == links.FolderLink(slug="abc-def")


# ── ranges ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("separator", ["-", " - ", "–"])
def test_a_range_is_two_links_however_it_is_spaced(separator: str) -> None:
    parsed = links.parse(f"https://t.me/a/10{separator}https://t.me/a/20")

    assert isinstance(parsed, links.RangeLink)
    assert (parsed.start.message_id, parsed.end.message_id) == (10, 20)
    assert parsed.count == 11, "inclusive at both ends"
    assert parsed.ids() == list(range(10, 21))


def test_a_reversed_range_is_ordered_rather_than_rejected() -> None:
    """The user meant the same messages; which end they typed first is not worth an error."""
    parsed = links.parse("https://t.me/a/20-https://t.me/a/10")

    assert isinstance(parsed, links.RangeLink)
    assert (parsed.start.message_id, parsed.end.message_id) == (10, 20)


def test_a_range_across_two_chats_is_refused() -> None:
    with pytest.raises(links.TgLinkError) as caught:
        links.parse("https://t.me/a/1-https://t.me/b/9")

    assert "one chat" in str(caught.value)


def test_a_dash_that_is_not_a_range_stays_a_single_message() -> None:
    """``t.me/a/1-2`` is one link with a stray tail, not a range: half of it is not a link."""
    with pytest.raises(links.TgLinkError):
        links.parse("https://t.me/a/1-2")


# ── refusals, which are the point of FR-4.6 ─────────────────────────────


def test_an_empty_link_asks_for_one() -> None:
    with pytest.raises(links.TgLinkError, match="No link was given"):
        links.parse("   ")


def test_another_site_is_refused_and_pointed_at_dl() -> None:
    with pytest.raises(links.TgLinkError) as caught:
        links.parse("https://example.com/a/1")

    assert "/dl" in str(caught.value)


def test_a_folder_link_is_not_a_message() -> None:
    with pytest.raises(links.TgLinkError, match="folder link names a whole folder"):
        links.parse_one("https://t.me/addlist/abc-def/5")
