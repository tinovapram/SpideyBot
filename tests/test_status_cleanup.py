"""The progress message must be deleted once the final report is sent."""

from __future__ import annotations

from typing import Any

from core.worker import _MessageStub, _TaskEvent
from utils.progress import StatusMessage


class _FakeClient:
    """Records edits, deletes and sends."""

    def __init__(self) -> None:
        self.edited: list[tuple[int, int, str]] = []
        self.deleted: list[tuple[int, list[int]]] = []
        self.sent: list[tuple[int, str]] = []

    async def edit_message(self, chat_id: int, msg_id: int, text: str) -> None:
        self.edited.append((chat_id, msg_id, text))

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        self.deleted.append((chat_id, message_ids))

    async def send_message(self, chat_id: int, text: str, **_kwargs: Any) -> Any:
        self.sent.append((chat_id, text))
        return _MessageStub(100 + len(self.sent), self, chat_id)


async def test_message_stub_delete_removes_the_status_message() -> None:
    client = _FakeClient()
    stub = _MessageStub(55, client, chat_id=7)

    await stub.delete()

    assert client.deleted == [(7, [55])]


async def test_stub_without_a_client_deletes_quietly() -> None:
    await _MessageStub(55, None, chat_id=7).delete()  # no client, no crash


async def test_finished_job_deletes_the_progress_message_and_resends_the_report() -> None:
    client = _FakeClient()
    event = _TaskEvent(client, chat_id=7, msg_id=55, command_msg_id=50)
    status = StatusMessage(event.message, _resend=event.new_status)
    status.set_header("📥 **SpideyBot:** Downloading & uploading")

    await status.cleanup_close("✅ **SpideyBot: Done!** Sent 3/3 files (1.20 MB).")

    assert client.deleted == [(7, [55])]  # the progress message is gone
    assert len(client.sent) == 1  # the report was re-sent once
    assert "Done!" in client.sent[0][1]
    assert "Downloading & uploading" not in client.sent[0][1]


async def test_failure_path_keeps_the_message_for_context() -> None:
    client = _FakeClient()
    event = _TaskEvent(client, chat_id=7, msg_id=55)
    status = StatusMessage(event.message, _resend=event.new_status)

    await status.cleanup_close("❌ **SpideyBot:** No files were sent.", edit_only=True)

    assert client.deleted == []  # errors stay in the progress message
    assert client.edited and "No files were sent" in client.edited[-1][2]
