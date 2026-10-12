"""AC-7: the Telegram path, end to end (FR-4, FR-13).

Two halves, tested together because they are one feature:

* the **handler** side — ``/dt`` refuses without a session, refuses a non-Telegram link, and
  queues a Telegram link; ``/login``/``/logout``/``/account`` are the session's surface;
* the **job** side — a queued Telegram link is fetched with the user's own session and delivered
  with its caption and its counts.

Only the client is faked. The database, the queue, the ledger, the gate and the delivery rules are
real, because all of them are what AC-7's three bullets actually assert.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import catalog, config, jobs, paths, queue, users, workers
from app import db as db_module
from app.config import Settings
from app.models import Job, JobItem
from handlers.account import account_command, logout_command
from handlers.start import start_command
from handlers.telegram_dl import dt_command
from telegram import sessions
from tests.fakes.messages import FakeClient, FakeMessage, FakeUser, RecordingClient

USER = 4242
CHAT = "channel"
CHAT_ID = -1001234
LINK = f"https://t.me/{CHAT}/5"
MB = 1024 * 1024


# ── what Telegram looks like from here ──────────────────────────────────


class TgFile:
    def __init__(self, *, size: int, name: str | None = None) -> None:
        self.file_size = size
        self.file_name = name


class TgMessage:
    """A message with media on it, in the shape ``telegram.media`` reads."""

    def __init__(
        self,
        message_id: int,
        *,
        kind: str | None = None,
        size: int = 1024,
        name: str | None = None,
        caption: str | None = None,
        group: str | None = None,
        text: str | None = None,
    ) -> None:
        self.id = message_id
        self.media = object() if kind else None
        self.caption = caption
        self.text = text
        self.media_group_id = group
        for attribute in ("photo", "video", "audio", "voice", "animation", "document"):
            setattr(self, attribute, None)
        if kind == "photo":
            self.photo = TgFile(size=size)
        elif kind == "video":
            self.video = TgFile(size=size, name=name or f"video-{message_id}.mp4")
        elif kind == "audio":
            self.audio = TgFile(size=size, name=name or f"audio-{message_id}.mp3")
        elif kind == "document":
            self.document = TgFile(size=size, name=name or f"file-{message_id}.bin")


class TgChat:
    def __init__(self, chat_id: int = CHAT_ID, title: str = "Test Channel") -> None:
        self.id = chat_id
        self.title = title
        self.username = CHAT


class TgSession:
    """The parts of a Kurigram client the Telegram path uses.

    Deliberately not a mock: it records what was asked for, so "the album came from one window
    fetch" and "nothing was downloaded because the gate refused" are observable facts, not beliefs.
    """

    def __init__(
        self,
        messages: dict[int, TgMessage] | None = None,
        *,
        folders: list[Any] | None = None,
        me_error: Exception | None = None,
        download_error: Exception | None = None,
        fail_ids: set[int] | None = None,
    ) -> None:
        self.is_connected = False
        self.is_initialized = False
        self.stopped = False
        self.logged_out = False
        self.messages = messages or {}
        self.folders = folders or []
        self.downloads: list[int] = []
        self.windows: list[list[int]] = []
        self._me_error = me_error
        self._download_error = download_error
        #: Message ids whose download fails, so "one file failed, the rest arrived" (FR-9.1) can be
        #: tested at the layer that produces the failure rather than at delivery.
        self._fail_ids = fail_ids or set()

    async def connect(self) -> bool:
        self.is_connected = True
        return True

    async def get_me(self) -> Any:
        if self._me_error is not None:
            raise self._me_error
        return "user"

    async def initialize(self) -> None:
        self.is_initialized = True

    async def stop(self) -> None:
        self.stopped = True
        self.is_connected = False

    async def log_out(self) -> None:
        self.logged_out = True

    async def get_chat(self, _chat_id: Any) -> Any:
        return TgChat()

    async def get_folders(self) -> list[Any]:
        return self.folders

    async def get_messages(self, _chat_id: Any, *, message_ids: Any = None, limit: int = 0) -> Any:
        if message_ids is None:
            ids = sorted(self.messages)[-limit:] if limit else []
            return [self.messages[i] for i in reversed(ids)]
        if isinstance(message_ids, int):
            return self.messages.get(message_ids)
        self.windows.append(list(message_ids))
        return [self.messages.get(i) for i in message_ids]

    async def download_media(self, message: TgMessage, *, file_name: str) -> str:
        if self._download_error is not None:
            raise self._download_error
        if message.id in self._fail_ids:
            raise RuntimeError(f"message {message.id} could not be downloaded")
        self.downloads.append(message.id)
        name = _name_of(message)
        target = Path(file_name) / name
        holder = message.photo or message.video or message.document or TgFile(size=4)
        target.write_bytes(b"x" * holder.file_size)
        return str(target)


def _name_of(message: TgMessage) -> str:
    for attribute in ("document", "video", "audio", "photo"):
        holder = getattr(message, attribute, None)
        if holder is not None:
            return getattr(holder, "file_name", None) or f"media-{message.id}"
    return f"media-{message.id}"


class Factory:
    def __init__(self, session: TgSession) -> None:
        self.session = session
        self.built = 0

    def __call__(self, _user_id: int) -> TgSession:
        self.built += 1
        return self.session


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def using(settings, monkeypatch, clean, tmp_path: Path):
    """Test settings, an emptied database, and a session directory of our own."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "SESSIONS_DIR", directory)
    monkeypatch.setattr(paths, "TEST_SESSIONS_DIR", directory / "test")
    monkeypatch.setattr(paths, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    sessions.set_pool(None)
    yield settings
    sessions.set_pool(None)


def install(settings: Settings, session: TgSession) -> TgSession:
    """Put a fake user client where the pool would put a real one (and a file, so it counts)."""
    sessions.set_pool(sessions.SessionPool(settings, factory=Factory(session)))
    paths.session_file(USER).parent.mkdir(parents=True, exist_ok=True)
    paths.session_file(USER).write_bytes(b"sqlite")
    return session


async def registered(user_id: int = USER) -> None:
    async with db_module.session() as session:
        await users.upsert(session, user_id=user_id, first_name="T")


async def run_telegram_job(
    settings: Settings,
    *,
    link: str,
    client: RecordingClient,
    tier: str = "free",
    user_id: int = USER,
) -> Job:
    """Queue a Telegram link, claim it, and run it exactly as the pool would."""
    await registered(user_id)
    async with db_module.session() as session:
        job = await queue.enqueue(session, user_id=user_id, link=link, tier=tier, site="telegram")
    async with db_module.session() as session:
        claimed = await queue.claim(session, settings)
        assert claimed is not None and claimed.id == job.id
        claimed.status_chat_id = user_id
        claimed.status_message_id = 4242

    handler = jobs.build_handler(settings, sender_for=lambda _: client, client=client)
    result = await workers.execute(job.id, settings, handler)
    assert result is not None

    async with db_module.session() as fresh:
        finished = await fresh.get(Job, job.id)
        assert finished is not None
    return finished


def dt_message(command: tuple[str, ...], user_id: int = USER) -> FakeMessage:
    return FakeMessage(FakeUser(user_id), command=command)


# ── AC-7: the handler's refusals ────────────────────────────────────────


async def test_ac7_dt_without_a_session_says_to_start_one(using) -> None:
    message = dt_message(("dt", LINK))

    await dt_command(FakeClient(), message)

    assert "session" in message.reply.lower()
    assert "/login" in message.reply
    assert "then send the link again" in message.reply
    assert await _jobs() == [], "nothing is queued for a session that cannot fetch it"


async def test_dt_refuses_another_site_and_points_at_dl(using) -> None:
    message = dt_message(("dt", "https://example.com/video/1"))

    await dt_command(FakeClient(), message)

    assert message.reply == catalog.DT_NOT_TELEGRAM


async def test_dt_reports_a_malformed_telegram_link_specifically(using) -> None:
    """FR-4.6: the parse error says what is wrong with *this* link."""
    message = dt_message(("dt", "https://t.me/c/not-a-number/5"))

    await dt_command(FakeClient(), message)

    assert "not a Telegram message link" in message.reply
    assert message.reply != catalog.DT_NOT_TELEGRAM


async def test_dt_without_an_argument_shows_the_usage(using) -> None:
    message = dt_message(("dt",))

    await dt_command(FakeClient(), message)

    assert message.reply == catalog.USAGE_DT


async def test_dt_with_a_session_queues_the_job_and_starts_its_status_message(using) -> None:
    install(using, TgSession())
    message = dt_message(("dt", LINK), user_id=USER)

    await dt_command(FakeClient(), message)

    assert "Queuing download" in message.reply
    jobs_stored = await _jobs()
    assert [job.link for job in jobs_stored] == [LINK]
    assert jobs_stored[0].site == "telegram"
    stored = await _job(jobs_stored[0].id)
    assert stored.status_message_id is not None, "the live status edits this message (FR-7.1)"


async def test_a_range_link_is_stored_in_a_form_that_parses_back(using) -> None:
    """A job row has to be enough to run the job after a restart."""
    install(using, TgSession())
    message = dt_message(("dt", "https://t.me/channel/10 - https://t.me/channel/12"))

    await dt_command(FakeClient(), message)

    stored = (await _jobs())[0]
    assert stored.link == "https://t.me/channel/10-https://t.me/channel/12"


async def test_a_dead_session_is_reported_as_needing_a_new_login(using) -> None:
    from pyrogram import errors

    install(using, TgSession(me_error=errors.AuthKeyUnregistered()))
    message = dt_message(("dt", LINK))

    await dt_command(FakeClient(), message)

    assert message.reply == catalog.DT_NO_SESSION
    assert not paths.session_file(USER).exists(), "the pool deletes what Telegram has revoked"
    assert await _jobs() == []


# ── AC-7: the delivery ──────────────────────────────────────────────────


async def test_ac7_a_message_link_delivers_its_media_with_the_caption_preserved(using) -> None:
    session = install(
        using,
        TgSession(
            {
                5: TgMessage(
                    5, kind="video", size=2048, caption="Holiday clip", name="clip.mp4"
                )
            }
        ),
    )
    client = RecordingClient()

    job = await run_telegram_job(using, link=LINK, client=client)

    assert job.status == queue.DELIVERED
    assert client.files == ["clip.mp4"]
    assert client.sent[0].caption == "Holiday clip", "FR-4.4 preserves the post's caption"
    assert session.downloads == [5]


async def test_ac7_the_final_message_reports_files_and_messages_scanned(using) -> None:
    install(
        using,
        TgSession(
            {
                10: TgMessage(10, kind="photo", size=512),
                11: TgMessage(11, text="just words"),
                12: TgMessage(12, kind="video", size=2048, caption="Second"),
            }
        ),
    )
    client = RecordingClient()

    await run_telegram_job(
        using, link="https://t.me/channel/10-https://t.me/channel/12", client=client
    )

    result = client.texts[-1]
    assert "Sent 2/2 file(s)" in result
    assert "scanned 3 message(s)" in result, "FR-4.5 counts messages looked at, not files"
    assert "Test Channel" in result, "and names the source chat"


async def test_a_photo_with_no_caption_gets_a_summary_one(using) -> None:
    install(using, TgSession({5: TgMessage(5, kind="photo", size=512)}))
    client = RecordingClient()

    await run_telegram_job(using, link=LINK, client=client)

    assert client.sent[0].caption == catalog.DT_CAPTION_FALLBACK.format(
        chat="Test Channel", id=5
    )


async def test_an_album_link_delivers_every_member_of_the_album(using) -> None:
    """A grouped post is one message plus its neighbours; the link points at one of them."""
    session = install(
        using,
        TgSession(
            {
                4: TgMessage(4, kind="photo", size=512, group="g1"),
                5: TgMessage(5, kind="photo", size=512, group="g1"),
                6: TgMessage(6, kind="video", size=1024, group="other", name="elsewhere.mp4"),
            }
        ),
    )
    client = RecordingClient()

    await run_telegram_job(using, link=LINK, client=client)

    assert sorted(client.files) == sorted(["media-4", "media-5"]), "the album, not the neighbour"
    assert session.downloads == [4, 5]


async def test_a_range_with_no_media_says_so_rather_than_reporting_an_error(using) -> None:
    install(using, TgSession({10: TgMessage(10, text="words"), 11: TgMessage(11)}))
    client = RecordingClient()

    job = await run_telegram_job(
        using, link="https://t.me/channel/10-https://t.me/channel/11", client=client
    )

    assert job.status == queue.FAILED
    assert client.files == []
    assert client.texts[-1] == catalog.DT_RANGE_EMPTY.format(
        start=10, end=11, chat=CHAT
    ), "the source's own sentence, not the generic error template"


async def test_a_message_that_does_not_exist_is_named(using) -> None:
    install(using, TgSession())
    client = RecordingClient()

    await run_telegram_job(using, link=LINK, client=client)

    assert client.texts[-1] == catalog.DT_MESSAGE_MISSING.format(id=5, chat=CHAT)


async def test_one_failed_file_does_not_stop_the_others(using) -> None:
    """FR-9.1: the rest of the range still arrives, and the failure is named."""
    install(
        using,
        TgSession(
            {
                10: TgMessage(10, kind="photo", size=512),
                11: TgMessage(11, kind="video", size=2048, name="ok.mp4"),
            },
            fail_ids={10},
        ),
    )
    client = RecordingClient()

    job = await run_telegram_job(
        using, link="https://t.me/channel/10-https://t.me/channel/11", client=client
    )

    assert client.files == ["ok.mp4"]
    assert job.status == queue.DELIVERED
    assert "Sent 1/2" in client.texts[-1], "the count is honest about the file that failed"
    assert "message 10" in client.texts[-1], "and the failure names which message it was"


async def test_an_oversized_telegram_link_is_refused_before_anything_is_downloaded(using) -> None:
    """AC-3 on the Telegram path: Telegram reports the size, so the gate can decide first."""
    session = install(using, TgSession({5: TgMessage(5, kind="video", size=600 * MB)}))
    client = RecordingClient()

    await run_telegram_job(using, link=LINK, client=client)

    assert session.downloads == [], "nothing was fetched"
    assert client.files == []
    assert client.texts[-1] == catalog.limit_exceeded_message(actual_mb="600", limit_mb="500")


async def test_a_delivered_telegram_file_is_written_to_the_ledger(using) -> None:
    """§8.2.3: the ledger is what stops a resumed job re-sending what already arrived."""
    install(using, TgSession({5: TgMessage(5, kind="video", size=1024, name="clip.mp4")}))

    job = await run_telegram_job(using, link=LINK, client=RecordingClient())

    async with db_module.session() as session:
        rows = (
            (await session.execute(select(JobItem).where(JobItem.job_id == job.id)))
            .scalars()
            .all()
        )
    assert [row.source_url for row in rows] == [f"tg:{CHAT}:5"]


# ── the session surface (FR-13) ─────────────────────────────────────────


async def _account(user_id: int = USER) -> FakeMessage:
    message = FakeMessage(FakeUser(user_id), command=("account",))
    await account_command(FakeClient(), message)
    return message


async def _logout(user_id: int = USER) -> FakeMessage:
    message = FakeMessage(FakeUser(user_id), command=("logout",))
    await logout_command(FakeClient(), message)
    return message


async def test_account_says_there_is_no_session(using) -> None:
    assert "🔴 No session" in (await _account()).reply


async def test_account_reports_a_stored_session_as_saved_but_not_running(using) -> None:
    install(using, TgSession())

    assert "🟡 Session saved (not running)" in (await _account()).reply


async def test_account_reports_a_connected_session(using) -> None:
    session = install(using, TgSession())
    await sessions.get_pool().get(USER)

    assert "🟢 Connected" in (await _account()).reply
    assert session.is_connected


async def test_logout_with_no_session_says_so_neutrally(using) -> None:
    reply = (await _logout()).reply

    assert reply == catalog.LOGOUT_NONE


async def test_logout_revokes_then_deletes(using) -> None:
    """D10: the server is told first; deleting the file alone leaves the session valid."""
    session = install(using, TgSession())

    reply = (await _logout()).reply

    assert session.logged_out, "log_out() was called on Telegram"
    assert session.stopped, "and the client was closed"
    assert not paths.session_file(USER).exists(), "the local session is gone"
    assert reply == catalog.LOGOUT_DONE


async def test_logout_reports_a_revocation_that_could_not_be_confirmed(using) -> None:
    """The session is gone locally either way, but the user is told what did not happen."""
    session = install(using, TgSession())
    session.log_out = _refuse  # type: ignore[method-assign]

    reply = (await _logout()).reply

    assert "could not be told to revoke" in reply
    assert not paths.session_file(USER).exists()


async def _refuse() -> None:
    raise RuntimeError("network down")


# ── §8.3: a restart reconnects from the file ────────────────────────────


async def test_start_reconnects_a_stored_session_and_says_it_is_connected(using) -> None:
    """The middle arrow of §8.3, and what FR-1.5's badge is for."""
    install(using, TgSession())
    message = FakeMessage(FakeUser(USER), command=("start",))

    await start_command(FakeClient(), message)

    assert "🟢 Connected" in message.reply


async def test_start_reports_a_session_that_is_stored_but_unreachable_as_saved(using) -> None:
    """A network problem is not a revocation: the file stays, and the badge says so."""
    sessions.set_pool(sessions.SessionPool(using, factory=Factory(_Unreachable())))
    paths.session_file(USER).parent.mkdir(parents=True, exist_ok=True)
    paths.session_file(USER).write_bytes(b"sqlite")
    message = FakeMessage(FakeUser(USER), command=("start",))

    await start_command(FakeClient(), message)

    assert "🟡 Session saved (not running)" in message.reply
    assert paths.session_file(USER).exists(), "a blip must not cost the user their login"


async def test_start_reports_a_revoked_session_as_no_session_and_removes_it(using) -> None:
    from pyrogram import errors

    install(using, TgSession(me_error=errors.AuthKeyUnregistered()))
    message = FakeMessage(FakeUser(USER), command=("start",))

    await start_command(FakeClient(), message)

    assert "🔴 No session" in message.reply
    assert not paths.session_file(USER).exists()


class _Unreachable(TgSession):
    """A session that exists but cannot be reached right now."""

    async def connect(self) -> bool:
        raise ConnectionError("network is down")


# ── helpers ─────────────────────────────────────────────────────────────


async def _jobs() -> list[Job]:
    async with db_module.session() as session:
        result = await session.execute(select(Job).order_by(Job.id))
        return list(result.scalars().all())


async def _job(job_id: int) -> Job:
    async with db_module.session() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        return job
