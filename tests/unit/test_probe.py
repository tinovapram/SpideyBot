"""The probe: engine routing, invocation, and parsing engine output.

The samples here are **trimmed from real output** captured against gallery-dl 1.32.15 and yt-dlp
2026.08.19, including the two shapes that would break a hand-written parser: a `[-1]` error record
under an exit code of 0, and a `ytdl:` marker whose media lives in the metadata instead. A test
written against a tidy invented shape would pass while the real one failed.
"""

from __future__ import annotations

import json

import pytest

from downloader import probe
from downloader.contracts import Resolution, ResolveError, safe_filename
from downloader.selection import Stream, choose, from_formats

MB = 1024 * 1024

# Real gallery-dl output for a Wikimedia file page, trimmed to two records.
WIKIMEDIA = json.dumps(
    [
        [2, {"category": "wikimediacommons", "page": "File:Example.jpg", "subcategory": "file"}],
        [
            3,
            "https://upload.wikimedia.org/wikipedia/commons/a/a9/Example.jpg"
            "?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=original",
            {
                "category": "wikimediacommons",
                "filename": "Example",
                "extension": "jpg",
                "num": 1,
                "count": 4,
                "size": 9022,
            },
        ],
    ]
)

# Real failure shape: exit code was 0 for this.
NOT_FOUND = json.dumps(
    [
        [
            -1,
            {
                "error": "HttpError",
                "message": "'404 Not Found' for 'https://api.imgur.com/post/v1/posts/exist'",
            },
        ]
    ]
)

UNSUPPORTED_EXIT = 64

# The same four candidates the selection tests use, as gallery-dl nests them under `formats`.
YTDL_INFO = {
    "title": "Big Buck Bunny 60fps 4K",
    "webpage_url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
    "formats": [
        {
            "format_id": "sb3",
            "url": "https://i.ytimg.com/sb/x/storyboard3_L0/default.jpg",
            "vcodec": "none",
            "acodec": "none",
            "ext": "jpg",
            "protocol": "mhtml",
        },
        {
            "format_id": "299",
            "url": "https://rr4/videoplayback?itag=299",
            "vcodec": "avc1.64002a",
            "acodec": "none",
            "ext": "mp4",
            "height": 1080,
            "filesize": 257643862,
            "protocol": "https",
        },
        {
            "format_id": "401",
            "url": "https://rr4/videoplayback?itag=401",
            "vcodec": "av01.0.13M.08",
            "acodec": "none",
            "ext": "mp4",
            "height": 2160,
            "filesize": 712445280,
            "protocol": "https",
        },
        {
            "format_id": "140",
            "url": "https://rr4/videoplayback?itag=140",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "ext": "m4a",
            "abr": 129.5,
            "filesize": 10275275,
            "protocol": "https",
        },
        {
            "format_id": "251",
            "url": "https://rr4/videoplayback?itag=251",
            "vcodec": "none",
            "acodec": "opus",
            "ext": "webm",
            "abr": 129.5,
            "filesize": 10275275,
            "protocol": "https",
        },
    ],
}

YTDL_MARKER = json.dumps(
    [
        [2, {"category": "youtube", "title": "Big Buck Bunny 60fps 4K"}],
        [3, "ytdl:https://www.youtube.com/watch?v=aqz-KE-bpKQ", YTDL_INFO],
    ]
)


# ── routing and invocation ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://music.youtube.com/watch?v=abc",
        "https://www.youtube-nocookie.com/embed/abc",
    ],
)
def test_the_youtube_family_goes_to_the_library_resolver(url: str) -> None:
    """gallery-dl can reach these, but only by returning a marker with no media in it."""
    assert probe.engine_for(url) == "ytdlp"


@pytest.mark.parametrize(
    "url",
    [
        "https://commons.wikimedia.org/wiki/File:Example.jpg",
        "https://imgur.com/gallery/abc",
        "https://twitter.com/user/status/1",
    ],
)
def test_everything_else_goes_to_gallery_dl(url: str) -> None:
    assert probe.engine_for(url) == "gallerydl"


def test_the_invocation_enables_the_ytdl_extractor() -> None:
    """Without this flag YouTube is 'Unsupported URL' and exit 64 — the whole feature is lost."""
    command = probe.gallerydl_command("https://example.test/x")

    assert probe.YTDL_EXTRACTOR_FLAG in command
    assert "--simulate" in command, "resolution must never write files"
    assert "-j" in command


def test_the_url_is_passed_as_an_argument_not_a_shell_string() -> None:
    """A URL with a space or a quote must not be able to become a second command."""
    command = probe.gallerydl_command("https://example.test/a b?x=1&y='2'")

    assert command[-1] == "https://example.test/a b?x=1&y='2'"


# ── parsing gallery-dl ──────────────────────────────────────────────────


def test_multiple_records_become_files() -> None:
    parsed = probe.parse_gallerydl(WIKIMEDIA, source_url="https://page.test/x")

    assert parsed.reason is None
    assert len(parsed.files) == 1
    file = parsed.files[0]
    assert file.filename == "Example.jpg"
    assert file.media_kind == "photo"
    assert file.filesize == 9022
    assert file.source_url == "https://page.test/x"
    assert not file.needs_merge
    # The resolved URL keeps its tracking parameters: stripping them would break signed links.
    assert file.primary.url.startswith("https://upload.wikimedia.org/")


def test_a_directory_record_is_not_media() -> None:
    parsed = probe.parse_gallerydl(json.dumps([[2, {"category": "x", "page": "y"}]]))

    assert parsed.files == ()
    # A share of folders and nothing else is E4, not a failure.
    assert parsed.empty is True


def test_an_error_record_is_reported_under_an_exit_code_of_zero() -> None:
    """The finding that makes the exit code useless as a success signal (R13)."""
    parsed = probe.parse_gallerydl(NOT_FOUND)

    assert parsed.files == ()
    assert parsed.reason is not None
    assert "404 Not Found" in parsed.reason and "api.imgur.com" in parsed.reason
    # The extractor named a failure, so this is E2 and its reason is worth repeating verbatim.
    assert parsed.empty is False


def test_unreadable_output_is_reported_not_raised() -> None:
    parsed = probe.parse_gallerydl("Traceback (most recent call last): ...")

    assert parsed.files == ()
    assert parsed.reason is not None and "unreadable" in parsed.reason
    assert parsed.empty is False


def test_empty_output_is_reported() -> None:
    parsed = probe.parse_gallerydl("   ")

    assert parsed.files == ()
    assert parsed.reason == "the extractor returned nothing"
    assert parsed.empty is True


def test_a_ytdl_marker_is_resolved_through_its_metadata() -> None:
    """The marker carries no media; the URLs are in the info dict gallery-dl embedded (R13)."""
    parsed = probe.parse_gallerydl(YTDL_MARKER)

    assert parsed.reason is None
    assert len(parsed.files) == 1
    file = parsed.files[0]
    assert file.needs_merge, "H.264 video plus AAC audio is two transfers"
    assert [transfer.role for transfer in file.transfers] == ["video", "audio"]
    assert all(not transfer.url.startswith("ytdl:") for transfer in file.transfers)
    assert file.playable is True
    assert file.filename.endswith(".mp4")


def test_a_marker_without_formats_yields_a_reason_not_a_crash() -> None:
    marker = json.dumps([[3, "ytdl:https://www.youtube.com/watch?v=x", {"title": "x"}]])

    parsed = probe.parse_gallerydl(marker)

    assert parsed.files == ()
    assert parsed.reason is not None


# ── parsing yt-dlp ──────────────────────────────────────────────────────


def test_an_info_dict_produces_one_file_with_two_transfers() -> None:
    files = probe.files_from_ytdlp(YTDL_INFO, source_url="https://www.youtube.com/watch?v=aqz")

    assert len(files) == 1
    file = files[0]
    assert file.needs_merge
    assert file.media_kind == "video"
    assert file.playable is True
    assert file.filename == "Big Buck Bunny 60fps 4K.mp4"
    assert file.expected_bytes == 257643862 + 10275275
    assert file.source_url == "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


def test_quality_preference_delivers_a_document_with_a_reason() -> None:
    """D28's fallback: the file is delivered, but not as a video that would fail to play."""
    files = probe.files_from_ytdlp(YTDL_INFO, preference="quality")

    assert len(files) == 1
    file = files[0]
    assert file.playable is False
    assert file.media_kind == "document"
    assert file.note is not None and "video codec" in file.note


def test_an_info_dict_with_nothing_fetchable_yields_nothing() -> None:
    only_storyboard = {**YTDL_INFO, "formats": [YTDL_INFO["formats"][0]]}

    assert probe.files_from_ytdlp(only_storyboard) == []


def test_an_entry_list_is_flattened() -> None:
    """A playlist arrives as entries; each one is a file."""
    playlist = {"entries": [{**YTDL_INFO, "title": "one"}, {**YTDL_INFO, "title": "two"}]}

    files = probe.files_from_ytdlp(playlist)

    assert [file.filename for file in files] == ["one.mp4", "two.mp4"]


def test_an_item_whose_stream_lost_its_url_is_dropped() -> None:
    """Never fetch a marker: a stream with no URL is not a candidate."""
    url_less = {
        **YTDL_INFO,
        "formats": [{key: value for key, value in YTDL_INFO["formats"][1].items() if key != "url"}],
    }

    assert probe.files_from_ytdlp(url_less) == []


# ── resolve: routing end to end, with the engines faked ─────────────────


async def test_resolve_uses_the_library_for_youtube() -> None:
    calls: list[str] = []

    async def fake_ytdlp(url: str, *, timeout: float, cookies_file=None):
        calls.append("ytdlp")
        return YTDL_INFO

    async def fake_gallerydl(
        url: str, *, timeout: float, cookies_file=None
    ):  # pragma: no cover - must not run
        calls.append("gallerydl")
        raise AssertionError("YouTube must not go through gallery-dl")

    resolution = await probe.resolve(
        "https://www.youtube.com/watch?v=aqz", run_ytdlp=fake_ytdlp, run_gallerydl=fake_gallerydl
    )

    assert calls == ["ytdlp"]
    assert resolution.engine == "ytdlp"
    assert bool(resolution) is True


async def test_resolve_uses_gallery_dl_for_other_hosts() -> None:
    calls: list[str] = []

    async def fake_gallerydl(url: str, *, timeout: float, cookies_file=None):
        calls.append("gallerydl")
        return WIKIMEDIA

    resolution = await probe.resolve(
        "https://commons.wikimedia.org/wiki/File:Example.jpg", run_gallerydl=fake_gallerydl
    )

    assert calls == ["gallerydl"]
    assert resolution.engine == "gallerydl"
    assert resolution.files[0].filename == "Example.jpg"


async def test_resolve_raises_the_extractors_own_reason() -> None:
    """A user should see why, not a traceback (FR-9)."""

    async def fake_gallerydl(url: str, *, timeout: float, cookies_file=None):
        return NOT_FOUND

    with pytest.raises(ResolveError) as raised:
        await probe.resolve("https://imgur.com/gallery/gone", run_gallerydl=fake_gallerydl)

    assert "404 Not Found" in str(raised.value)
    # An error the extractor named is a failure (E2), not an empty share (E4).
    assert raised.value.empty is False


async def test_resolve_marks_a_folder_only_share_as_empty() -> None:
    """E4 gets its own message, so the flow must be able to tell it from a failure."""

    async def fake_gallerydl(url: str, *, timeout: float, cookies_file=None):
        return json.dumps([[2, {"category": "x", "page": "y"}]])

    with pytest.raises(ResolveError) as raised:
        await probe.resolve("https://example.test/share", run_gallerydl=fake_gallerydl)

    assert raised.value.empty is True


async def test_resolve_reports_an_empty_resolution_for_video_links() -> None:
    async def fake_ytdlp(url: str, *, timeout: float, cookies_file=None):
        return {"title": "x", "formats": []}

    with pytest.raises(ResolveError) as raised:
        await probe.resolve("https://youtu.be/x", run_ytdlp=fake_ytdlp)

    assert raised.value.empty is True


async def test_a_timeout_is_a_resolve_error_not_a_hang() -> None:
    async def slow_ytdlp(url: str, *, timeout: float, cookies_file=None):
        raise ResolveError(f"the video resolver did not answer within {timeout:.0f}s")

    with pytest.raises(ResolveError) as raised:
        await probe.resolve("https://youtu.be/x", timeout=5, run_ytdlp=slow_ytdlp)

    assert "5s" in str(raised.value)


# ── the contract's own rules ────────────────────────────────────────────


def test_an_unknown_size_is_not_an_empty_file() -> None:
    """None must not read as zero, or the size gate would wave through an empty download (E3)."""
    from downloader.contracts import ResolvedFile, Transfer

    unknown = ResolvedFile(transfers=(Transfer(url="u"),), filename="x.mp4")
    known = ResolvedFile(transfers=(Transfer(url="u", filesize=0),), filename="x.mp4")

    assert unknown.expected_bytes is None
    assert known.expected_bytes == 0, "a reported zero is a measurement, not a missing value"


def test_a_merge_with_one_unknown_size_has_no_usable_total() -> None:
    """A partial sum would look precise and could pass a gate the real download fails (AC-3)."""
    from downloader.contracts import ResolvedFile, Transfer

    partial = ResolvedFile(
        transfers=(Transfer(url="v", role="video", filesize=1000), Transfer(url="a", role="audio")),
        filename="x.mp4",
    )

    assert partial.expected_bytes is None


def test_selection_does_not_invent_a_size() -> None:
    """`approx_bytes` is all-or-nothing for the same reason `expected_bytes` is."""
    from downloader.selection import Selection

    assert Selection(video=Stream(format_id="1", url="u", vcodec="avc1")).approx_bytes is None
    assert (
        Selection(video=Stream(format_id="1", url="u", vcodec="avc1", filesize=0)).approx_bytes == 0
    )


def test_a_resolution_total_is_unknown_if_any_file_size_is() -> None:
    """A partial sum would look precise and could pass a gate the real download fails (AC-3)."""
    from downloader.contracts import ResolvedFile, Transfer

    one = ResolvedFile(transfers=(Transfer(url="u", filesize=100),), filename="a")
    two = ResolvedFile(transfers=(Transfer(url="u"),), filename="b")

    assert Resolution("gallerydl", "x", (one,)).total_bytes == 100
    assert Resolution("gallerydl", "x", (one, two)).total_bytes is None
    assert Resolution("gallerydl", "x", ()).total_bytes is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("normal title.mp4", "normal title.mp4"),
        ("  spaced   out  ", "spaced out"),
        ("", "spideybot-download"),
        ("...", "spideybot-download"),
        ("line\nbreak", "line_break"),
        ("a/b\\c:d*e?f", "a_b_c_d_e_f"),
    ],
)
def test_filenames_are_cleaned(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


@pytest.mark.parametrize("raw", ["../../etc/passwd", r"..\..\windows\system32", "/absolute/path"])
def test_a_title_cannot_produce_a_path(raw: str) -> None:
    """Titles come from the internet, so a separator in one must not write outside the staging
    directory."""
    cleaned = safe_filename(raw)

    assert "/" not in cleaned
    assert "\\" not in cleaned
    assert not cleaned.startswith((".", " ")) or cleaned == "spideybot-download"


def test_a_very_long_title_is_capped() -> None:
    from downloader.contracts import MAX_FILENAME_LENGTH

    assert len(safe_filename("x" * 500)) == MAX_FILENAME_LENGTH


def test_kinds_are_decided_by_extension() -> None:
    from downloader.contracts import kind_for

    assert kind_for("JPG") == "photo"
    assert kind_for(".mp4") == "video"
    assert kind_for("opus") == "audio"
    assert kind_for("zip") == "document"
    assert kind_for(None) == "document"


def test_selection_and_probe_agree_on_the_default_pair() -> None:
    """The resolver must not pick a different pair from the policy it is supposed to follow."""
    info_files = probe.files_from_ytdlp(YTDL_INFO)
    policy = choose(from_formats(YTDL_INFO["formats"]))

    assert policy is not None
    assert info_files[0].primary.url == policy.video.url
    assert info_files[0].transfers[1].url == policy.audio.url
