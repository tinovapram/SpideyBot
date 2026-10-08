"""Choosing which streams to fetch for a video, and whether the result will play (D28).

Every YouTube format is a merge now — the probe behind this found **no** single-file format at all
(0 of 53 on one video, 0 of 24 on another) — so this module decides what a user actually receives,
not merely how it is transferred.

The default is **compatibility-first**: H.264 video with AAC audio in MP4. It plays in every
Telegram client, merges by stream copy, and is about 2.7x smaller than the best available pair
(255 MB against 689 MB on the probed video). The size is the argument that matters most: the daily
bandwidth cap, not the download count, is what really limits a free user, and the smaller pick is
also what fits inside `link_total_mb`.

Quality-first is one config value away (`[download] video_preference = "quality"`) for an operator
who would rather have 2160p AV1 and accept both the bytes and the playback risk.

Two traps this module exists to close, both observed in real output rather than imagined:

* ``formats`` contains **non-media entries** — storyboards with ``protocol: "mhtml"`` and
  ``vcodec``/``acodec`` of ``"none"``. A naive "largest format" pick can select one.
* A stream we have no URL for is not a candidate at all, however attractive its metadata. That is
  what stops a `ytdl:`-marked item from being "fetched" as if it were media (R13).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

Preference = Literal["compatible", "quality"]

#: Formats that describe how the *page* is presented rather than media we could fetch. A
#: storyboard is an image grid, and `mhtml` is the archived-page pseudo-protocol.
NON_MEDIA_PROTOCOLS = frozenset({"mhtml", "data"})

#: Codec prefixes. `avc1` is H.264, `mp4a` is AAC — the pair every client plays.
COMPATIBLE_VIDEO_CODEC = "avc1"
COMPATIBLE_AUDIO_CODEC = "mp4a"

CONTAINER_MP4 = "mp4"
CONTAINER_MKV = "mkv"


@dataclass(frozen=True)
class Stream:
    """One candidate, reduced to what the choice needs."""

    format_id: str
    url: str | None = None
    vcodec: str | None = None
    acodec: str | None = None
    ext: str | None = None
    height: int | None = None
    filesize: int | None = None
    protocol: str | None = None
    bitrate: float | None = None

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec) and self.vcodec != "none"

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec) and self.acodec != "none"

    @property
    def is_progressive(self) -> bool:
        """Video and audio in one file, so no merge is needed."""
        return self.has_video and self.has_audio

    @property
    def is_media(self) -> bool:
        return (self.has_video or self.has_audio) and self.protocol not in NON_MEDIA_PROTOCOLS

    @property
    def is_fetchable(self) -> bool:
        return self.is_media and bool(self.url)

    @property
    def is_compatible_video(self) -> bool:
        return (self.vcodec or "").startswith(COMPATIBLE_VIDEO_CODEC)

    @property
    def is_compatible_audio(self) -> bool:
        return (self.acodec or "").startswith(COMPATIBLE_AUDIO_CODEC)

    def rank(self) -> tuple[int, float]:
        """Video quality proxy: resolution first, then bitrate. Never the file size, which
        rewards a codec for being inefficient."""
        return (self.height or 0, self.bitrate or 0.0)


@dataclass(frozen=True)
class Selection:
    """What to fetch, and how the result will be delivered."""

    video: Stream
    audio: Stream | None = None
    container: str = CONTAINER_MP4
    #: Whether Telegram will play this inline. False means deliver it as a document, since a file
    #: that will not play is not a successful *video* download (D28).
    playable: bool = True
    reason: str | None = None

    @property
    def streams(self) -> tuple[Stream, ...]:
        return (self.video,) if self.audio is None else (self.video, self.audio)

    @property
    def needs_merge(self) -> bool:
        return self.audio is not None

    @property
    def approx_bytes(self) -> int:
        return sum(stream.filesize or 0 for stream in self.streams)


def from_formats(raw: Iterable[Mapping[str, Any]]) -> list[Stream]:
    """Convert engine format dicts into candidates.

    Tolerant on purpose: yt-dlp and gallery-dl disagree about which keys they populate
    (``filesize`` against ``filesize_approx``, ``tbr`` against ``abr``), and a missing field
    should cost that stream nothing but its tie-break.
    """
    streams: list[Stream] = []
    for entry in raw:
        streams.append(
            Stream(
                format_id=str(entry.get("format_id") or ""),
                url=entry.get("url"),
                vcodec=entry.get("vcodec"),
                acodec=entry.get("acodec"),
                ext=entry.get("ext") or entry.get("extension"),
                height=entry.get("height"),
                filesize=entry.get("filesize") or entry.get("filesize_approx"),
                protocol=entry.get("protocol"),
                bitrate=entry.get("tbr") or entry.get("abr"),
            )
        )
    return streams


def _best(candidates: Sequence[Stream]) -> Stream | None:
    return max(candidates, key=Stream.rank) if candidates else None


def _best_audio(candidates: Sequence[Stream]) -> Stream | None:
    """The audio stream to merge in, preferring AAC for container compatibility.

    Preference is for AAC even alongside an incompatible video: if the video is AV1 and the result
    can only be a document anyway, the audio should still be the most widely playable track on
    offer rather than the one a default pick would happen to pair with it.
    """
    if not candidates:
        return None
    compatible = [stream for stream in candidates if stream.is_compatible_audio]
    pool = compatible or candidates
    return max(pool, key=lambda stream: stream.bitrate or 0.0)


def _playback_reason(video: Stream, audio: Stream | None) -> str | None:
    """Why this pair will not play inline, or ``None`` when it will.

    Names the codec that is actually at fault, so a caption or a log line says which one to blame
    rather than reporting the pair as a whole.
    """
    problems: list[str] = []
    if not video.is_compatible_video:
        problems.append(f"video codec {video.vcodec}")
    if audio is not None and not audio.is_compatible_audio:
        problems.append(f"audio codec {audio.acodec}")

    if not problems:
        return None
    if len(problems) == 1:
        return f"{problems[0]} is not universally playable"
    return " and ".join(problems) + " are not universally playable"


def choose(formats: Sequence[Stream], *, preference: Preference = "compatible") -> Selection | None:
    """Pick the streams to fetch, or ``None`` when nothing here can be fetched.

    Returns a :class:`Selection` whose ``playable`` flag tells the caller whether to deliver a
    video or fall back to a document, rather than failing the job: the user asked for the file,
    and an unplayable-but-complete file is more useful than an error.
    """
    fetchable = [stream for stream in formats if stream.is_fetchable]
    videos = [stream for stream in fetchable if stream.has_video]

    video: Stream | None
    if preference == "compatible":
        video = _best([stream for stream in videos if stream.is_compatible_video]) or _best(videos)
    else:
        video = _best(videos)

    if video is None:
        # Audio-only items are legitimate (a SoundCloud track, a podcast clip).
        audio_only = _best_audio([stream for stream in fetchable if stream.has_audio])
        if audio_only is None:
            return None
        return Selection(
            video=audio_only,
            container=audio_only.ext or "m4a",
            playable=True,
            reason="audio only",
        )

    if video.is_progressive:
        return Selection(
            video=video,
            container=video.ext or CONTAINER_MP4,
            playable=video.is_compatible_video and video.is_compatible_audio,
            reason=_playback_reason(video, None),
        )

    audio = _best_audio(
        [stream for stream in fetchable if stream.has_audio and not stream.has_video]
    )
    if audio is None:
        # A video with no audio anywhere: still a complete video file, just silent.
        return Selection(
            video=video,
            container=video.ext or CONTAINER_MP4,
            playable=video.is_compatible_video,
            reason="no audio stream available"
            if video.is_compatible_video
            else _playback_reason(video, None),
        )

    playable = video.is_compatible_video and audio.is_compatible_audio
    return Selection(
        video=video,
        audio=audio,
        container=CONTAINER_MP4 if playable else CONTAINER_MKV,
        playable=playable,
        reason=_playback_reason(video, audio),
    )
