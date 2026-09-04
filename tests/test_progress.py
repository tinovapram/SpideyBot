"""Tests for utils.progress — unified StatusMessage and line builders."""

import asyncio

from utils.progress import StatusMessage, bytes_line, count_line


class FakeMessage:
    """Minimal stand-in for a Telethon message that records edits."""

    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit(self, text: str, **kwargs) -> None:
        self.edits.append(text)


class TestLineBuilders:
    def test_bytes_line_full(self):
        line = bytes_line("📥", "Downloading", 50, 100)
        assert "📥" in line
        assert "Downloading" in line
        assert "50%" in line

    def test_bytes_line_unknown_total(self):
        line = bytes_line("📤", "Uploading", 500, 0)
        assert "Uploading" in line
        assert "500.0 B" in line

    def test_count_line_with_total(self):
        assert count_line("📤", "Sent", 3, 5) == "📤 **Sent:** 3/5"

    def test_count_line_without_total(self):
        assert count_line("📥", "Downloaded", 2) == "📥 **Downloaded:** 2"


class TestStatusMessage:
    def test_compose_header_rows_footer(self):
        status = StatusMessage(object(), footer="Send /cancel 1")
        status.set_header("📦 **Test task**")
        status.row("a", "line A")
        status.row("b", "line B")
        text = status._compose()
        assert "📦 **Test task**" in text
        assert "line A" in text
        assert "line B" in text
        assert "Send /cancel 1" in text

    def test_drop_row(self):
        status = StatusMessage(object())
        status.row("x", "keep")
        status.row("y", "remove")
        status.drop("y")
        assert "remove" not in status._compose()

    def test_empty_row_drops(self):
        status = StatusMessage(object())
        status.row("x", "keep")
        status.row("y", "")
        assert "keep" in status._compose()
        assert "y" not in status._rows

    def test_bytes_cb_updates_row(self):
        status = StatusMessage(object())
        cb = status.bytes_cb("dl", "📥", "Downloading")
        cb(50, 100)
        assert "dl" in status._rows
        assert "50%" in status._rows["dl"]

    def test_file_bytes_cb_accepts_three_args(self):
        """TeraBox downloaders call cb(filename, done, total) — must not raise."""
        status = StatusMessage(object())
        cb = status.file_bytes_cb("cur", "📥")
        cb("GYA VC (2).mp4", 25, 100)
        row = status._rows["cur"]
        assert "GYA VC (2).mp4" in row
        assert "25%" in row

    def test_file_bytes_cb_truncates_long_filename(self):
        status = StatusMessage(object())
        cb = status.file_bytes_cb("cur", "📥")
        long_name = "A" * 120 + ".mp4"
        cb(long_name, 10, 40)
        assert "25%" in status._rows["cur"]
        assert "A" * 120 not in status._rows["cur"]
        assert "..." in status._rows["cur"]

    def test_animate_while_rows_present(self):
        status = StatusMessage(object(), header="📥 Downloading")
        status.row("dl", "row")
        text = status._compose()
        # header carries at least one animated dot while a row is live
        assert text.startswith("📥 Downloading.")

    def test_no_animation_without_rows(self):
        status = StatusMessage(object(), header="Resolving")
        assert status._compose() == "Resolving"

    def test_close_replaces_message_and_stops_updates(self):
        async def scenario():
            msg = FakeMessage()
            status = StatusMessage(msg, header="title")
            status.row("a", "working")
            await status.close("final ✅")
            status.set_header("should not render")
            await asyncio.sleep(0)
            return msg.edits

        edits = asyncio.run(scenario())
        assert edits and edits[-1] == "final ✅"
