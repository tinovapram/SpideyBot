"""The media-selection policy (D28).

The format dicts here are the real ones: the ids, codecs, heights and sizes are what a live
`yt-dlp -j` returned for the probed video, including the storyboard entry and the absence of any
single-file format. That is deliberate — the traps this policy exists to avoid were found in real
output, not invented, and a test written against a tidy invented format list would not catch them.
"""

from __future__ import annotations

import pytest

from app.config import build_settings
from download.selection import (
    CONTAINER_MKV,
    CONTAINER_MP4,
    Selection,
    Stream,
    choose,
    from_formats,
)

MB = 1024 * 1024

# Real entries, trimmed to the fields that matter.
STORYBOARD = {
    "format_id": "sb3",
    "url": "https://i.ytimg.com/sb/aqz-KE-bpKQ/storyboard3_L0/default.jpg?sigh=rs$AOn4",
    "vcodec": "none",
    "acodec": "none",
    "ext": "jpg",
    "protocol": "mhtml",
    "filesize": None,
}
H264_1080 = {
    "format_id": "299",
    "url": "https://rr4---sn-.../videoplayback?itag=299",
    "vcodec": "avc1.64002a",
    "acodec": "none",
    "ext": "mp4",
    "height": 1080,
    "filesize": 257643862,
    "protocol": "https",
    "tbr": 3246.0,
}
AV1_2160 = {
    "format_id": "401",
    "url": "https://rr4---sn-.../videoplayback?itag=401",
    "vcodec": "av01.0.13M.08",
    "acodec": "none",
    "ext": "mp4",
    "height": 2160,
    "filesize": 712445280,
    "protocol": "https",
    "tbr": 8984.0,
}
AAC_AUDIO = {
    "format_id": "140",
    "url": "https://rr4---sn-.../videoplayback?itag=140",
    "vcodec": "none",
    "acodec": "mp4a.40.2",
    "ext": "m4a",
    "abr": 129.5,
    "filesize": 10275275,
    "protocol": "https",
}
OPUS_AUDIO = {
    "format_id": "251",
    "url": "https://rr4---sn-.../videoplayback?itag=251",
    "vcodec": "none",
    "acodec": "opus",
    "ext": "webm",
    "abr": 129.5,
    "filesize": 10275275,
    "protocol": "https",
}
PROGRESSIVE_360 = {
    "format_id": "18",
    "url": "https://rr4---sn-.../videoplayback?itag=18",
    "vcodec": "avc1.42001E",
    "acodec": "mp4a.40.2",
    "ext": "mp4",
    "height": 360,
    "filesize": 20 * MB,
    "protocol": "https",
    "tbr": 600.0,
}

FULL_LIST = [STORYBOARD, H264_1080, AV1_2160, AAC_AUDIO, OPUS_AUDIO]


def _candidates(*entries: dict) -> list[Stream]:
    return from_formats(entries)


# ── compatibility-first, the default ────────────────────────────────────


def test_compatible_prefers_h264_over_higher_resolution_av1() -> None:
    """The whole point of the policy: a lower resolution that plays beats one that may not."""
    selection = choose(_candidates(*FULL_LIST), preference="compatible")

    assert selection is not None
    assert selection.video.format_id == "299"
    assert selection.audio is not None and selection.audio.format_id == "140"
    assert selection.playable is True
    assert selection.container == CONTAINER_MP4
    assert selection.needs_merge is True


def test_the_compatible_pick_is_much_smaller() -> None:
    """The measurable argument for D28: about 2.7x fewer bytes than yt-dlp's own choice."""
    selection = choose(_candidates(*FULL_LIST), preference="compatible")

    assert selection is not None
    assert selection.approx_bytes == 257643862 + 10275275
    assert selection.approx_bytes < 280 * MB, "fits a free tier's 500 MB link limit"


def test_quality_takes_the_best_resolution_and_admits_it_may_not_play() -> None:
    """Quality-first is available, but it does not pretend the result will stream."""
    selection = choose(_candidates(*FULL_LIST), preference="quality")

    assert selection is not None
    assert selection.video.format_id == "401"
    # AAC rather than yt-dlp's Opus partner: if the video is AV1 and the result is a document
    # anyway, the audio should still be the most widely playable track on offer.
    assert selection.audio is not None and selection.audio.format_id == "140"
    assert selection.playable is False, "AV1 is not a codec every client plays"
    assert selection.container == CONTAINER_MKV
    assert selection.reason == "video codec av01.0.13M.08 is not universally playable"


def test_the_reason_blames_the_codec_actually_at_fault() -> None:
    """A caption or log line has to say which side is the problem."""
    av1_video = choose(_candidates(AV1_2160, AAC_AUDIO), preference="quality")
    opus_audio = choose(_candidates(H264_1080, OPUS_AUDIO), preference="compatible")
    both = choose(_candidates(AV1_2160, OPUS_AUDIO), preference="quality")

    assert av1_video is not None and av1_video.reason is not None
    assert "video codec" in av1_video.reason and "audio codec" not in av1_video.reason

    assert opus_audio is not None and opus_audio.reason is not None
    assert "audio codec" in opus_audio.reason and "video codec" not in opus_audio.reason

    assert both is not None and both.reason is not None
    assert "video codec" in both.reason and "audio codec" in both.reason


# ── the traps ───────────────────────────────────────────────────────────


def test_a_storyboard_is_never_selected_even_if_it_looks_attractive() -> None:
    """`formats` contains non-media entries; a naive largest-wins pick can choose one."""
    oversized_storyboard = {**STORYBOARD, "filesize": 999 * MB}
    selection = choose(_candidates(oversized_storyboard, H264_1080, AAC_AUDIO))

    assert selection is not None
    assert selection.video.format_id == "299", "a storyboard is not media"


def test_a_stream_without_a_url_is_not_a_candidate() -> None:
    """This is what stops a `ytdl:`-marked item being 'fetched' as if its marker were media."""
    url_less = {**AV1_2160, "url": None}
    selection = choose(_candidates(url_less, H264_1080, AAC_AUDIO), preference="quality")

    assert selection is not None
    assert selection.video.format_id == "299", "unfetchable, however good it looks"


def test_storyboard_only_input_yields_no_selection() -> None:
    assert choose(_candidates(STORYBOARD)) is None


def test_nothing_fetchable_yields_no_selection() -> None:
    assert choose([]) is None


# ── the awkward cases ───────────────────────────────────────────────────


def test_a_progressive_stream_is_used_without_a_merge() -> None:
    """When a single file is the best video available, there is nothing to merge."""
    selection = choose(_candidates(PROGRESSIVE_360))

    assert selection is not None
    assert selection.needs_merge is False
    assert selection.playable is True
    assert selection.container == CONTAINER_MP4


def test_no_h264_at_all_falls_back_and_marks_it_unplayable() -> None:
    """Sub-decision: deliver the file as a document rather than failing the job."""
    av1_only = {**AV1_2160}
    selection = choose(_candidates(av1_only, OPUS_AUDIO), preference="compatible")

    assert selection is not None
    assert selection.video.format_id == "401", "better than refusing the link"
    assert selection.playable is False


def test_h264_with_opus_cannot_be_mp4() -> None:
    """Remuxing Opus into MP4 is not standard, so the pair is delivered as a document."""
    selection = choose(_candidates(H264_1080, OPUS_AUDIO), preference="compatible")

    assert selection is not None
    assert selection.playable is False
    assert selection.container == CONTAINER_MKV


def test_a_video_with_no_audio_still_delivers() -> None:
    """A silent video is a complete file; failing it would be wrong."""
    selection = choose(_candidates(H264_1080), preference="compatible")

    assert selection is not None
    assert selection.audio is None
    assert selection.needs_merge is False
    assert selection.playable is True
    assert selection.reason == "no audio stream available"


def test_an_audio_only_item_is_a_legitimate_selection() -> None:
    selection = choose(_candidates(AAC_AUDIO))

    assert selection is not None
    assert selection.video.format_id == "140"
    assert selection.needs_merge is False
    assert selection.playable is True
    assert selection.reason == "audio only"


def test_aac_is_preferred_over_opus_for_the_merge() -> None:
    selection = choose(_candidates(H264_1080, OPUS_AUDIO, AAC_AUDIO), preference="compatible")

    assert selection is not None
    assert selection.audio is not None
    assert selection.audio.format_id == "140"


# ── conversion from engine output ───────────────────────────────────────


def test_filesize_approx_and_abr_are_accepted_as_fallbacks() -> None:
    """yt-dlp and gallery-dl disagree about which keys they populate."""
    streams = from_formats([{"format_id": "x", "filesize_approx": 123, "abr": 64.0}])

    assert streams[0].filesize == 123
    assert streams[0].bitrate == 64.0


def test_extension_is_accepted_under_either_key() -> None:
    """gallery-dl reports `extension`, and sets it to None for `ytdl:` items."""
    streams = from_formats([{"format_id": "x", "extension": "mp4"}, {"format_id": "y"}])

    assert streams[0].ext == "mp4"
    assert streams[1].ext is None


def test_height_beats_bitrate_when_ranking() -> None:
    """Resolution is the quality users notice; a high-bitrate low-res stream is not better."""
    small_but_heavy = Stream(format_id="a", url="u", vcodec="avc1", height=480, bitrate=9000.0)
    large = Stream(format_id="b", url="u", vcodec="avc1", height=1080, bitrate=2000.0)

    assert large.rank() > small_but_heavy.rank()


def test_the_shipped_default_is_compatibility_first() -> None:
    """The config value and the code default must not drift apart."""
    settings = build_settings(tg_api_id="1", tg_api_hash="a" * 32, tg_bot_token="1:fake")
    selection = choose(_candidates(*FULL_LIST), preference=settings.download.video_preference)

    assert selection is not None
    assert selection.video.format_id == "299"


@pytest.mark.parametrize("preference", ["compatible", "quality"])
def test_every_preference_yields_a_fetchable_pair(preference: str) -> None:
    selection = choose(_candidates(*FULL_LIST), preference=preference)  # type: ignore[arg-type]

    assert isinstance(selection, Selection)
    assert all(stream.url for stream in selection.streams)
