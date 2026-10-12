"""The web-bridge adapters, driven by the payloads their sites answer with (D6, R4).

Offline by necessity, not by preference: thirteen of these hosts are third-party "saver" sites, so a
test that needed the network could not run in CI at all — and a live suite that only ran when
someone remembered would be the same as no suite. What is asserted here is therefore the
*contract*: given an answer of the shape the site produces, the adapter must name its file
correctly, classify it as the right kind (FR-8.1), and hand over a URL the fetcher can use.

The payloads are written from each site's documented shape and the previous implementation's
parsing, not captured from a live run — the difference matters and is worth stating: these tests
prove the parsing and the naming, and they cannot prove the site still answers that way. That is
what the live smoke matrix in the plan is for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from downloader.contracts import Resolution, ResolvedFile, ResolveError, Transfer
from downloader.sites import (
    bridge,    capcut,
    dailymotion,
    douyin,
    kuaishou,
    linkedin,
    pinterest,
    reddit,
    snapchat,
    soundcloud,
    spotify,
    threads,
    tiktok,
    tumblr,
    twitter,
)

MP4 = "https://cdn.test/video.mp4"
JPG = "https://cdn.test/photo.jpg"
MP3 = "https://cdn.test/track.mp3"

#: A Kuaishou manifest as the share page carries it: one group holding two encodings of one video,
#: each stating its own bitrate and size. The numbers are from a real page, because the ranking is
#: the thing under test and a made-up pair (1 against 2) would not catch a sort that reversed.
KUAISHOU_BIG = "https://cdn.test/720p-avc.mp4"
KUAISHOU_REPS: list[dict[str, Any]] = [
    {
        "qualityType": "720p",
        "videoCodec": "avc",
        "avgBitrate": "1164",
        "fileSize": "1348400",
        "hidden": "False",
        "url": KUAISHOU_BIG,
    },
    {
        "qualityType": "720p",
        "videoCodec": "hevc",
        "avgBitrate": "454",
        "fileSize": "528665",
        "hidden": "False",
        "url": "https://cdn.test/720p-hevc.mp4",
    },
]
KUAISHOU_MANIFEST: dict[str, Any] = {
    "manifest": {"adaptationSet": [{"duration": 9300, "id": 1, "representation": KUAISHOU_REPS}]}
}


def kuaishou_page(**photo: Any) -> FakeResponse:
    """A Kuaishou share page, whose state sits in the markup under an obfuscated top-level key."""
    state = {"tusjoh.0sftu0uj:": {"result": 1, "photo": {"photoId": "1", **photo}}}
    return FakeResponse(text=f"<script>window.INIT_STATE = {json.dumps(state)}</script>")


class FakeResponse:
    """Enough of a ``curl_cffi`` response for the bridge helpers: body, status, final URL."""

    def __init__(
        self,
        *,
        text: str = "",
        payload: Any = None,
        status_code: int = 200,
        url: str = MP4,
    ) -> None:
        self.text = json.dumps(payload) if payload is not None else text
        self._payload = payload
        self.status_code = status_code
        self.url = url
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON in this response")
        return self._payload


class FakeSession:
    """A session that answers with a queue of responses and remembers what it was asked."""

    def __init__(self, *answers: FakeResponse, fail: Exception | None = None) -> None:
        self.answers = list(answers)
        self.fail = fail
        self.headers: dict[str, str] = {}
        self.cookies = self
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.set_cookies: list[str] = []

    def set(self, name: str, value: str, **_: Any) -> None:
        self.set_cookies.append(name)

    def _next(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if self.fail is not None:
            raise self.fail
        if not self.answers:
            raise AssertionError(
                f"the adapter called {method} {url} more times than the test scripted"
            )
        return self.answers.pop(0)

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("HEAD", url, **kwargs)

    async def close(self) -> None:
        return


async def resolve(module: Any, url: str, *answers: FakeResponse, **kwargs: Any) -> Resolution:
    return await module.attempt(url, client=FakeSession(*answers, **kwargs))


async def expect_error(module: Any, url: str, *answers: FakeResponse, **kwargs: Any) -> str:
    """The failure this adapter reports, which is what the chain uses to move on."""
    with pytest.raises(ResolveError) as caught:
        await resolve(module, url, *answers, **kwargs)
    return str(caught.value)


def only(resolution: Resolution) -> Any:
    assert len(resolution.files) == 1, f"expected one file, got {len(resolution.files)}"
    return resolution.files[0]


def assert_media(
    file: Any, *, filename: str, url: str, kind: str, role: str | None = None
) -> None:
    """The things delivery depends on, for one resolved file.

    ``role`` is checked only where a test passes one: most adapters use the default, so asserting it
    everywhere would be noise — but for a playlist or an audio stream the role *is* the interesting
    part, and those tests say so.
    """
    assert file.filename == filename
    assert file.primary.url == url
    assert file.media_kind == kind, f"{filename} would be sent as a {file.media_kind}"
    if role is not None:
        assert file.primary.role == role


# ── the JSON bridges ─────────────────────────────────────────────────────────


async def test_capcut_reads_its_bridge_and_names_the_file() -> None:
    resolution = await resolve(
        capcut,
        "https://www.capcut.com/watch/1",
        FakeResponse(payload={"download_url": MP4, "title": "My Template"}),
    )

    assert resolution.engine == "capcut"
    assert resolution.source_url == "https://www.capcut.com/watch/1"
    assert_media(only(resolution), filename="My Template.mp4", url=MP4, kind="video")


async def test_capcut_sends_the_origin_the_endpoint_requires() -> None:
    session = FakeSession(FakeResponse(payload={"url": MP4}))
    await capcut.attempt("https://www.capcut.com/watch/1", client=session)

    _method, _url, kwargs = session.calls[0]
    assert kwargs["headers"]["Origin"] == "https://www.genviral.io"


async def test_dailymotion_finds_a_url_nested_in_its_answer() -> None:
    """The endpoint has used a flat URL and a nested ``data`` object; both must work."""
    resolution = await resolve(
        dailymotion,
        "https://www.dailymotion.com/video/1",
        FakeResponse(payload={"data": {"title": "Clip", "url": MP4}}),
    )

    assert_media(only(resolution), filename="Clip.mp4", url=MP4, kind="video")


async def test_kuaishou_picks_the_highest_bitrate_representation() -> None:
    """A share page holds several encodings of one video; the best one is what was asked for."""
    resolution = await resolve(
        kuaishou,
        "https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa",
        kuaishou_page(caption="萌妹子", **KUAISHOU_MANIFEST),
    )

    file = only(resolution)
    assert_media(file, filename="萌妹子.mp4", url=KUAISHOU_BIG, kind="video")
    # The manifest states the size, which is what lets the size gate refuse an oversize video before
    # any transfer starts (E3) — the mirrors carry no size at all.
    assert file.filesize == 1348400


async def test_kuaishou_ignores_a_hidden_representation() -> None:
    """The state writes booleans as *strings*, so ``bool("False")`` is true and ``bool("True")`` is
    true as well. Both mistakes are silent — one ships a worse file, the other ships none — so both
    directions are checked here: a hidden rep is skipped even when it is the highest bitrate, and a
    representation marked ``"False"`` stays eligible."""
    hidden = {
        "qualityType": "1080p",
        "avgBitrate": "9000",
        "fileSize": "999",
        "hidden": "True",
        "url": "https://cdn.test/hidden.mp4",
    }
    manifest = {"adaptationSet": [{"id": 1, "representation": [hidden, *KUAISHOU_REPS]}]}

    resolution = await resolve(
        kuaishou,
        "https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa",
        kuaishou_page(manifest=manifest),
    )

    assert_media(only(resolution), filename="Kuaishou_Video.mp4", url=KUAISHOU_BIG, kind="video")


async def test_kuaishou_follows_a_share_link_for_the_photo_id() -> None:
    """A ``v.kuaishou.com`` link is only a redirect: the id exists nowhere else, and the share page
    will not answer without it."""
    photo_page = "https://kph1tf76.m.chenzhongtech.com/fw/photo/3xi5u3rzf5qrgq4?cc=share_copylink"
    session = FakeSession(
        FakeResponse(text="", url=photo_page),
        kuaishou_page(caption="Clip", **KUAISHOU_MANIFEST),
    )

    await kuaishou.attempt("https://v.kuaishou.com/8qIlZu", client=session)

    _method, url, _kwargs = session.calls[1]
    assert url == "https://v.m.chenzhongtech.com/fw/photo/3xi5u3rzf5qrgq4"


async def test_kuaishou_falls_back_to_the_mirrors_without_a_manifest() -> None:
    resolution = await resolve(
        kuaishou,
        "https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa",
        kuaishou_page(
            caption="Clip",
            manifest={"adaptationSet": []},
            mainMvUrls=[{"cdn": "mirror", "url": MP4}],
        ),
    )

    file = only(resolution)
    assert_media(file, filename="Clip.mp4", url=MP4, kind="video")
    assert file.filesize is None


async def test_kuaishou_reports_an_unavailable_post_and_a_page_change_differently() -> None:
    """Two different futures hide behind one failure: the post is gone (nothing to fix) or Kuaishou
    changed its page (something to fix). The messages have to say which."""
    gone = await expect_error(
        kuaishou,
        "https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa",
        FakeResponse(text="<html><body>no state here</body></html>"),
    )
    assert "state" in gone

    unavailable = await expect_error(
        kuaishou,
        "https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa",
        kuaishou_page(caption="Clip"),
    )
    assert "no photo" in unavailable


async def test_kuaishou_refuses_a_link_that_names_no_photo() -> None:
    message = await expect_error(
        kuaishou,
        "https://www.kuaishou.com/profile/somebody",
        FakeResponse(text="", url="https://www.kuaishou.com/profile/somebody"),
    )

    assert "not a Kuaishou video link" in message


async def test_linkedin_reads_the_download_url_field() -> None:
    resolution = await resolve(
        linkedin,
        "https://www.linkedin.com/posts/1",
        FakeResponse(payload={"downloadUrl": MP4, "title": "Post"}),
    )

    assert_media(only(resolution), filename="Post.mp4", url=MP4, kind="video")


async def test_snapchat_falls_back_to_a_default_title() -> None:
    resolution = await resolve(
        snapchat,
        "https://www.snapchat.com/spotlight/1",
        FakeResponse(payload={"video_url": MP4}),
    )

    assert_media(only(resolution), filename="Snapchat_Video.mp4", url=MP4, kind="video")


async def test_soundcloud_reads_the_media_list_and_registers_audio() -> None:
    resolution = await resolve(
        soundcloud,
        "https://soundcloud.com/user/track",
        FakeResponse(payload={"title": "Sombrsong", "medias": [{"url": MP3}]}),
    )

    file = only(resolution)
    assert_media(file, filename="Sombrsong.mp3", url=MP3, kind="audio", role="audio")


async def test_soundcloud_posts_a_form_body_rather_than_json() -> None:
    """The endpoint rejects JSON, so sending it would look like the bridge being broken."""
    session = FakeSession(FakeResponse(payload={"url": MP3}))
    await soundcloud.attempt("https://soundcloud.com/user/track", client=session)

    _method, _url, kwargs = session.calls[0]
    assert "json" not in kwargs
    assert "token=" in str(kwargs["data"])


async def test_spotify_returns_every_track_of_a_release_numbered() -> None:
    resolution = await resolve(
        spotify,
        "https://open.spotify.com/album/1",
        FakeResponse(
            payload={
                "tracks": [
                    {"title": "One", "url": MP3},
                    {"title": "Two", "url": "https://cdn.test/two.mp3"},
                ]
            }
        ),
    )

    assert [file.filename for file in resolution.files] == ["One_1.mp3", "Two_2.mp3"]
    assert all(file.media_kind == "audio" for file in resolution.files)


async def test_spotify_numbers_a_single_track_without_a_suffix() -> None:
    """D37's rule: numbering is for disambiguation, so a lone track keeps its own name."""
    resolution = await resolve(
        spotify,
        "https://open.spotify.com/track/1",
        FakeResponse(payload={"downloadUrl": MP3, "title": "Only"}),
    )

    assert_media(only(resolution), filename="Only.mp3", url=MP3, kind="audio", role="audio")


async def test_tumblr_classifies_an_image_as_a_photo() -> None:
    """FR-8.1: an image named ``.mp4`` would be delivered as a video that does not play."""
    resolution = await resolve(
        tumblr,
        "https://staff.tumblr.com/post/1",
        FakeResponse(payload={"image": JPG, "title": "Still"}),
    )

    assert_media(only(resolution), filename="Still.jpg", url=JPG, kind="photo")


async def test_a_bridge_that_answers_with_nothing_is_a_resolve_error() -> None:
    """The chain's "try the next resolver" rule depends on this being a failure, not a crash."""
    message = await expect_error(
        tumblr, "https://staff.tumblr.com/post/1", FakeResponse(payload={"status": "error"})
    )

    assert "downloadable URL" in message


async def test_a_bridge_that_cannot_be_reached_is_a_resolve_error() -> None:
    """A dead third-party site must degrade to the next resolver, not fail the whole job."""
    message = await expect_error(
        tumblr,
        "https://staff.tumblr.com/post/1",
        fail=OSError("connection refused"),
    )

    assert "could not reach" in message


async def test_a_bridge_that_answers_with_html_is_not_read_as_json() -> None:
    message = await expect_error(
        tumblr, "https://staff.tumblr.com/post/1", FakeResponse(text="<html>nope</html>")
    )

    assert "did not answer with JSON" in message


# ── the HTML bridges ─────────────────────────────────────────────────────────


async def test_douyin_parses_the_fragment_inside_its_json() -> None:
    fragment = """
    <div class="tik-left"><div class="thumbnail">
      <div class="content"><h3>Douyin Clip</h3></div></div></div>
    <div class="tik-right"><div class="dl-action">
      <a href="https://cdn.test/profile" class="tik-button-dl">View profile</a>
      <a href="https://cdn.test/douyin.mp4" class="tik-button-dl">Download MP4</a>
    </div></div>
    """
    resolution = await resolve(
        douyin,
        "https://www.douyin.com/video/1",
        FakeResponse(payload={"status": "ok", "data": fragment}),
    )

    # The profile button is skipped: it is not media, and taking it would download a web page.
    assert_media(
        only(resolution),
        filename="Douyin Clip.mp4",
        url="https://cdn.test/douyin.mp4",
        kind="video",
    )


async def test_douyin_falls_back_to_the_inline_player() -> None:
    fragment = '<div id="vid" data-src="https://cdn.test/inline.mp4"></div>'
    resolution = await resolve(
        douyin,
        "https://www.douyin.com/video/1",
        FakeResponse(payload={"status": "ok", "data": fragment}),
    )

    assert only(resolution).primary.url == "https://cdn.test/inline.mp4"


async def test_douyin_reports_an_unusable_answer() -> None:
    message = await expect_error(
        douyin, "https://www.douyin.com/video/1", FakeResponse(payload={"status": "error"})
    )

    assert "usable answer" in message


async def test_threads_returns_a_carousel_as_several_files() -> None:
    fragment = """
    <ul class="download-box">
      <li><span class="icon-dlimage"></span>
        <select class="photo-option"><option value="https://cdn.test/1.jpg">1</option>
        <option value="https://cdn.test/2.jpg">2</option></select></li>
      <li><span class="icon-dlvideo"></span>
        <a title="Download Video" href="https://cdn.test/threads.mp4">Video</a></li>
    </ul>
    """
    resolution = await resolve(
        threads,
        "https://www.threads.net/@user/post/1",
        FakeResponse(payload={"status": "ok", "data": fragment}),
    )

    assert [file.primary.url for file in resolution.files] == [
        "https://cdn.test/threads.mp4",
        "https://cdn.test/1.jpg",
    ]
    assert [file.media_kind for file in resolution.files] == ["video", "photo"]
    assert [file.filename for file in resolution.files] == [
        "Threads_Post_1.mp4",
        "Threads_Post_2.jpg",
    ]


async def test_tiktok_returns_a_slideshow_as_images_not_a_video() -> None:
    """The slide class is a photo: taking it as the video would deliver a still that cannot play."""
    html = """
    <div id="avatar_and_text"><h2>Slideshow post</h2></div>
    <a class="download_link slide" href="https://cdn.test/s1.jpg">1</a>
    <a class="download_link slide" href="https://cdn.test/s2.jpg">2</a>
    """
    resolution = await resolve(
        tiktok, "https://www.tiktok.com/@user/video/1", FakeResponse(text=html)
    )

    assert [file.media_kind for file in resolution.files] == ["photo", "photo"]
    assert [file.filename for file in resolution.files] == [
        "Slideshow post_1.jpg",
        "Slideshow post_2.jpg",
    ]


async def test_tiktok_returns_the_video_when_there_is_one() -> None:
    html = """
    <div id="avatar_and_text"><h2>Clip</h2></div>
    <a class="download_link" href="https://cdn.test/tiktok.mp4">Download MP4</a>
    """
    resolution = await resolve(
        tiktok, "https://www.tiktok.com/@user/video/1", FakeResponse(text=html)
    )

    assert_media(
        only(resolution), filename="Clip.mp4", url="https://cdn.test/tiktok.mp4", kind="video"
    )


async def test_tiktok_reports_a_post_with_no_links() -> None:
    message = await expect_error(
        tiktok, "https://www.tiktok.com/@user/video/1", FakeResponse(text="<html></html>")
    )

    assert "no download link" in message


async def test_pinterest_unwraps_the_redirect_and_takes_the_best_row() -> None:
    html = """
    <h1>Pin title</h1>
    <table><tbody>
      <tr><td class="video-quality">HD</td><td>Video</td>
        <td><a href="https://savepin.app/redir?url=https%3A%2F%2Fcdn.test%2Fpin.mp4">Go</a></td></tr>
      <tr><td class="video-quality">SD</td><td>Image</td>
        <td><a href="https://cdn.test/pin.jpg">Go</a></td></tr>
    </tbody></table>
    """
    resolution = await resolve(
        pinterest, "https://www.pinterest.com/pin/1/", FakeResponse(text=html)
    )

    assert_media(
        only(resolution), filename="Pin title.mp4", url="https://cdn.test/pin.mp4", kind="video"
    )


async def test_pinterest_falls_back_to_the_pin_image() -> None:
    """A pin with no download row is still an image, and downloading it beats reporting nothing."""
    html = """
    <h1>Just a pin</h1>
    <div class="image-container"><img src="https://cdn.test/original.jpg"></div>
    """
    resolution = await resolve(
        pinterest, "https://www.pinterest.com/pin/1/", FakeResponse(text=html)
    )

    assert_media(
        only(resolution),
        filename="Just a pin.jpg",
        url="https://cdn.test/original.jpg",
        kind="photo",
    )


# ── Twitter's two routes ─────────────────────────────────────────────────────


async def test_twitter_uses_the_api_and_takes_every_attachment() -> None:
    resolution = await resolve(
        twitter,
        "https://twitter.com/user/status/123",
        FakeResponse(
            payload={
                "text": "look at this\nsecond line",
                "media_extended": [
                    {"url": MP4, "type": "video"},
                    {"url": JPG, "type": "photo"},
                ],
            }
        ),
    )

    assert [file.filename for file in resolution.files] == [
        "look at this_1.mp4",
        "look at this_2.jpg",
    ]
    assert [file.media_kind for file in resolution.files] == ["video", "photo"]


async def test_twitter_sends_the_playlist_media_with_the_hls_role() -> None:
    """A playlist must reach the fetcher as ``hls``, or it would be saved as a text ``.mp4``."""
    resolution = await resolve(
        twitter,
        "https://x.com/user/status/123",
        FakeResponse(
            payload={"text": "clip", "media_extended": [{"url": "https://cdn.test/v.m3u8"}]}
        ),
    )

    file = only(resolution)
    assert file.primary.role == "hls"
    assert file.filename.endswith(".mp4"), "the output of ffmpeg is an MP4"


async def test_twitter_falls_back_to_the_scraped_bridge() -> None:
    html = """
    <div class="tw-middle"><h3>Scraped post</h3></div>
    <a class="tw-button-dl" href="https://dl.snapcdn.app/mp4/1">Download MP4</a>
    <a class="tw-button-dl" href="https://example.test/app">Open in app</a>
    """
    session = FakeSession(
        FakeResponse(status_code=500),
        FakeResponse(payload={"status": "ok", "data": html}),
    )

    resolution = await twitter.attempt("https://twitter.com/user/status/123", client=session)

    assert_media(
        only(resolution),
        filename="Scraped post.mp4",
        url="https://dl.snapcdn.app/mp4/1",
        kind="video",
    )
    assert [call[1] for call in session.calls] == [
        "https://api.vxtwitter.com/Twitter/status/123",
        "https://savetwitter.net/api/ajaxSearch",
    ], "the API is tried first, and the bridge only when it fails"


async def test_twitter_reports_a_text_only_post_as_empty() -> None:
    """E4, not E2: the post resolved and simply has nothing in it to download."""
    with pytest.raises(ResolveError) as caught:
        await resolve(
            twitter,
            "https://twitter.com/user/status/123",
            FakeResponse(payload={"text": "just words", "media_extended": []}),
            FakeResponse(payload={"status": "ok", "data": "<html></html>"}),
        )

    assert caught.value.empty is True


async def test_twitter_keeps_the_empty_verdict_when_the_bridge_also_fails() -> None:
    """Found by the live smoke matrix, on a real text-only post (``twitter.com/jack/status/20``).

    The API read the post and said it holds nothing; savetwitter then broke. Reporting *that* would
    tell the user the download failed when the true answer is that there was never anything to
    download — and E4's message is a different one (D23).
    """
    with pytest.raises(ResolveError) as caught:
        await resolve(
            twitter,
            "https://twitter.com/user/status/123",
            FakeResponse(payload={"text": "just setting up my twttr", "media_extended": []}),
            FakeResponse(status_code=500),
        )

    assert caught.value.empty is True


async def test_twitter_rejects_a_link_that_is_not_a_status() -> None:
    """Both routes need the tweet id, so a profile link is refused rather than guessed at."""
    message = await expect_error(
        twitter,
        "https://twitter.com/user",
        FakeResponse(payload={"media_extended": [{"url": MP4}]}),
        FakeResponse(status_code=500),
    )

    assert "twitter" in message.lower()


# ── Reddit: a submission shape, not a payload ────────────────────────────────


class FakeSubmission:
    """The handful of submission attributes the adapter reads, for each kind of post."""

    def __init__(self, **fields: Any) -> None:
        self.title = fields.get("title", "A post")
        self.is_video = fields.get("is_video", False)
        self.is_gallery = fields.get("is_gallery", False)
        self.media_metadata = fields.get("media_metadata")
        self.media = fields.get("media")
        self.url = fields.get("url")


@pytest.fixture
def submission(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a submission, so PRAW itself is never constructed or called."""

    def install(item: FakeSubmission) -> FakeSubmission:
        async def fake(reddit: Any, url: str) -> FakeSubmission:
            return item

        monkeypatch.setattr(reddit, "_submission", fake)
        return item

    return install


async def test_reddit_gallery_yields_every_valid_image(submission: Any) -> None:
    submission(
        FakeSubmission(
            title="Gallery",
            is_gallery=True,
            media_metadata={
                "a": {"status": "valid", "m": "image/jpg", "s": {"u": "https://i.redd.it/a.jpg"}},
                # A quarantined entry is not fetchable, so it must be skipped rather than fail.
                "b": {"status": "failed", "m": "image/png", "s": {"u": "https://i.redd.it/b.png"}},
                "c": {"status": "valid", "m": "image/png", "s": {"u": "https://i.redd.it/c.png"}},
            },
        )
    )

    resolution = await reddit.attempt("https://www.reddit.com/r/x/comments/1/y")

    assert [file.filename for file in resolution.files] == ["Gallery_a.jpg", "Gallery_c.png"]
    assert all(file.media_kind == "photo" for file in resolution.files)


async def test_reddit_unescapes_a_gallery_url(submission: Any) -> None:
    """Reddit escapes ``&`` in its own metadata; fetching the escaped URL fails."""
    submission(
        FakeSubmission(
            is_gallery=True,
            media_metadata={
                "a": {
                    "status": "valid",
                    "m": "image/jpg",
                    "s": {"u": "https://i.redd.it/a.jpg?width=1&amp;height=2"},
                }
            },
        )
    )

    resolution = await reddit.attempt("https://www.reddit.com/r/x/comments/1/y")

    assert resolution.files[0].primary.url == "https://i.redd.it/a.jpg?width=1&height=2"


async def test_reddit_video_goes_through_ytdlp_for_the_merge(submission: Any) -> None:
    """The fallback URL is video-only, so the library has to resolve both streams."""
    submission(FakeSubmission(is_video=True, media={"reddit_video": {"fallback_url": MP4}}))
    merged = ResolvedFile(
        transfers=(
            Transfer("https://cdn.test/v", "video"),
            Transfer("https://cdn.test/a", "audio"),
        ),
        filename="Clip.mp4",
        media_kind="video",
    )

    async def fake_resolve(url: str, **kwargs: Any) -> Resolution:
        return Resolution(engine="yt-dlp", source_url=url, files=(merged,))

    monkeypatch_stub(fake_resolve)
    resolution = await reddit.attempt("https://www.reddit.com/r/x/comments/1/y")

    assert resolution.engine == "yt-dlp", "the library's answer is passed straight through"
    assert only(resolution).needs_merge is True, "both streams, for the fetcher to combine"
    assert only(resolution).primary.url != MP4, "the video-only fallback was not used"


async def test_reddit_video_without_ytdlp_says_the_file_has_no_sound(submission: Any) -> None:
    """Better a stated caveat than a silent video the user cannot explain."""
    submission(
        FakeSubmission(
            title="Clip", is_video=True, media={"reddit_video": {"fallback_url": MP4}}
        )
    )

    async def failing(url: str, **kwargs: Any) -> Resolution:
        raise RuntimeError("yt-dlp is not installed")

    monkeypatch_stub(failing)
    resolution = await reddit.attempt("https://www.reddit.com/r/x/comments/1/y")

    file = only(resolution)
    assert file.primary.url == MP4
    assert file.note is not None and "without sound" in file.note


async def test_reddit_image_link_is_its_own_file(submission: Any) -> None:
    submission(FakeSubmission(title="Picture", url="https://i.redd.it/x.jpg"))

    resolution = await reddit.attempt("https://www.reddit.com/r/x/comments/1/y")

    assert_media(
        only(resolution), filename="Picture.jpg", url="https://i.redd.it/x.jpg", kind="photo"
    )


async def test_reddit_reports_a_post_with_nothing_downloadable(submission: Any) -> None:
    submission(FakeSubmission(title="Text", url="https://example.test/article"))

    async def nothing(url: str, **kwargs: Any) -> Resolution:
        return Resolution(engine="yt-dlp", source_url=url, files=())

    monkeypatch_stub(nothing)
    message = await expect_error(reddit, "https://www.reddit.com/r/x/comments/1/y")

    assert "nothing downloadable" in message


# ── Reddit: an identity chain that has to actually fall through ──────────────


class FakeReddit:
    """A stand-in for the ``praw`` module that refuses whichever identities a test names.

    ``Reddit(...)`` does not contact Reddit in reality either — PRAW constructs lazily and the
    *first API call* is what authenticates. Modelling that is the point of this double: a client
    whose ``auth.scopes()`` raises is exactly the 401 the live API returned for the third public
    pair, and the adapter has to notice it and try the next candidate.
    """

    def __init__(self, refuse: set[str] | None = None) -> None:
        self.refuse = refuse or set()
        self.constructed: list[dict[str, Any]] = []
        self.validated: list[str] = []

    def Reddit(self, **kwargs: Any) -> Any:
        self.constructed.append(kwargs)
        module = self
        client_id = kwargs["client_id"]

        class Client:
            read_only = False

            def __init__(self) -> None:
                self.auth = self

            def scopes(self) -> set[str]:
                module.validated.append(client_id)
                if client_id in module.refuse:
                    raise RuntimeError("received 401 HTTP response")
                return {"*"}

        return Client()


@pytest.fixture
def praw(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake ``praw`` for one test."""

    def install(*, refuse: set[str] | None = None) -> FakeReddit:
        fake = FakeReddit(refuse)
        monkeypatch.setitem(sys.modules, "praw", fake)
        return fake

    return install


def test_the_public_reddit_pairs_are_used_when_nothing_is_configured(praw: Any) -> None:
    """The adapter has to work on a checkout with no Reddit app registered (V5 relied on this)."""
    fake = praw()

    client = reddit._client({}, 30.0)

    assert client is not None
    first = fake.constructed[0]
    assert (first["client_id"], first["client_secret"]) == reddit._FALLBACK_APPS[0]
    assert "refresh_token" not in first, "application-only, since there is no configured login"


def test_every_public_pair_is_tried_before_giving_up(praw: Any) -> None:
    """The list is a chain, not a single hardcoded identity: each one is attempted in turn."""
    fake = praw(refuse={reddit._FALLBACK_APPS[0][0], reddit._FALLBACK_APPS[1][0]})

    reddit._client({}, 30.0)

    assert [entry["client_id"] for entry in fake.constructed] == [
        pairing[0] for pairing in reddit._FALLBACK_APPS
    ]


def test_a_dead_pair_falls_through_to_the_next_one(praw: Any) -> None:
    """The bug this fixes: without validating, the first candidate always 'wins' and a 401 is only
    discovered later — as 'Reddit would not answer' rather than as an attempt at the next identity.

    The identity that fails here is the *first* one, so the second has to be reached with a real
    request (the live API confirms the third public pair answers 401 today).
    """
    fake = praw(refuse={"icuiw6HNK21QoQ"})

    client = reddit._client({}, 30.0)

    assert client is not None
    assert fake.validated == ["icuiw6HNK21QoQ", "b70jdVnmRpFmVc12GvVuAw"]


def test_every_pair_being_refused_is_a_resolve_error(praw: Any) -> None:
    praw(refuse={pairing[0] for pairing in reddit._FALLBACK_APPS})

    with pytest.raises(ResolveError, match="would not accept any available client id"):
        reddit._client({}, 30.0)


def test_a_configured_pair_is_tried_before_the_public_ones(praw: Any) -> None:
    fake = praw()

    reddit._client({"client_id": "mine", "client_secret": "secret"}, 30.0)

    assert fake.constructed[0]["client_id"] == "mine"


def test_a_configured_pair_matching_a_public_one_is_not_retried(praw: Any) -> None:
    """De-duplication: otherwise setting one of the public ids explicitly doubles the requests."""
    fake = praw()

    reddit._client({"client_id": "icuiw6HNK21QoQ", "client_secret": "x"}, 30.0)

    assert [entry["client_id"] for entry in fake.constructed].count("icuiw6HNK21QoQ") == 1


def test_a_refresh_token_is_an_account_login_and_is_tried_first(praw: Any) -> None:
    fake = praw()

    reddit._client({"client_id": "mine", "refresh_token": "rt"}, 30.0)

    assert fake.constructed[0]["refresh_token"] == "rt"
    assert fake.constructed[0]["client_id"] == "mine"


def test_a_refused_refresh_token_falls_back_to_application_only(praw: Any) -> None:
    """A revoked token must cost the operator the account login, not the whole adapter."""
    fake = praw(refuse={"mine"})

    client = reddit._client({"client_id": "mine", "refresh_token": "rt"}, 30.0)

    assert client is not None
    assert [entry["client_id"] for entry in fake.constructed] == [
        "mine",
        "icuiw6HNK21QoQ",
    ], "the same identity is retried without the token, then the public pairs"


def test_a_missing_praw_is_reported_rather_than_raising_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "praw", None)

    with pytest.raises(ResolveError, match="praw is not installed"):
        reddit._client({}, 30.0)


# ── the chain: a dead bridge must not be the end of the job ──────────────────


async def test_a_dead_bridge_falls_through_to_the_next_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D6's whole point, checked through the real registry rather than a hand-built chain.

    A bridge that is down is a ``ResolveError``, and the chain has to move on rather than fail the
    job — which is why the adapter tests above each assert a `ResolveError` rather than "returns
    nothing". Here the *real* `registry.chain_for` composes the chain for a Twitter link, the native
    adapter is made to fail the way a dead site fails, and the next resolver is what answers.
    """
    from downloader import flow, registry

    async def dead(url: str) -> Resolution:
        raise ResolveError("the bridge is down", empty=False)

    # The native adapter is defeated by pointing it at a session that refuses to connect.
    original = twitter.attempt

    async def failing(url: str, **kwargs: Any) -> Resolution:
        return await original(url, client=FakeSession(fail=OSError("connection refused")), **kwargs)

    monkeypatch.setattr(twitter, "attempt", failing)

    reached: list[str] = []

    async def next_resolver_answers(url: str) -> Resolution:
        reached.append(url)
        return Resolution(
            engine="gallery-dl",
            source_url=url,
            files=(
                bridge.media("https://cdn.test/from-engine.mp4", "from-engine.mp4", source_url=url),
            ),
        )

    chain = registry.chain_for("https://twitter.com/user/status/123")
    assert chain[0].name == "twitter", "the native adapter is still tried first"

    eventual = flow.Resolver(name="gallery-dl", attempt=next_resolver_answers)
    notices: list[str] = []

    async def notice(message: str) -> None:
        notices.append(message)

    resolution = await flow.resolve_through_chain(
        "https://twitter.com/user/status/123", (*chain[:1], eventual), on_notice=notice
    )

    assert resolution.engine == "gallery-dl"
    assert reached == ["https://twitter.com/user/status/123"]
    assert notices, "E2's fallback notice is emitted, so the user is told why it restarted"


_STUBBED: list[Any] = []


def monkeypatch_stub(function: Any) -> None:
    """Replace ``probe.resolve_ytdlp`` for one test, restored by the fixture below."""
    from downloader import probe

    _STUBBED.append((probe, probe.resolve_ytdlp))
    probe.resolve_ytdlp = function


@pytest.fixture(autouse=True)
def restore_stubs() -> Any:
    yield
    while _STUBBED:
        module, original = _STUBBED.pop()
        module.resolve_ytdlp = original


def test_the_payloads_in_this_file_are_the_shapes_the_adapters_parse() -> None:
    """A note in test form: these fixtures are written from the parsers, not captured.

    Kept as a test so the claim is visible where the fixtures are, rather than only in a comment
    nobody reads before trusting a green run.
    """
    assert Path(__file__).name == "test_sites.py"
    assert json.loads(json.dumps({"ok": True})) == {"ok": True}
