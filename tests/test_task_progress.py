"""
Tests for spideybot.utils.task_progress — progress tracking, state transitions, message building.
"""

import pytest
from spideybot.utils.task_progress import FileStatus, FileProgress, TaskProgress


class TestFileStatus:
    """Tests for the FileStatus enum."""

    def test_all_statuses_exist(self):
        assert FileStatus.PENDING.value == "pending"
        assert FileStatus.DOWNLOADING.value == "downloading"
        assert FileStatus.UPLOADING.value == "uploading"
        assert FileStatus.SENT.value == "sent"
        assert FileStatus.FAILED.value == "failed"
        assert FileStatus.SKIPPED.value == "skipped"


class TestFileProgress:
    """Tests for the FileProgress dataclass."""

    def test_default_values(self):
        fp = FileProgress(index=0, filename="test.mp4")
        assert fp.filename == "test.mp4"
        assert fp.status == FileStatus.PENDING
        assert fp.total_bytes == 0
        assert fp.current_bytes == 0
        assert fp.error is None

    def test_percent_property_zero_total(self):
        fp = FileProgress(index=0, filename="test.mp4")
        assert fp.percent == 0.0

    def test_percent_property_with_bytes(self):
        fp = FileProgress(index=0, filename="test.mp4", total_bytes=1000, current_bytes=500)
        assert fp.percent == 50.0

    def test_percent_property_complete(self):
        fp = FileProgress(index=0, filename="test.mp4", total_bytes=100, current_bytes=100)
        assert fp.percent == 100.0


class TestTaskProgress:
    """Tests for the TaskProgress class lifecycle."""

    def test_create_with_files(self):
        tp = TaskProgress(title="Test Download", total_files=3)
        assert tp.title == "Test Download"
        assert tp.total_files == 3

    def test_add_file(self):
        tp = TaskProgress(title="Test", total_files=2)
        tp.add_file(0, "video.mp4", total_bytes=1024)
        fp = tp._get(0)
        assert fp.filename == "video.mp4"
        assert fp.total_bytes == 1024
        assert fp.status == FileStatus.PENDING

    def test_mark_downloading(self):
        tp = TaskProgress(title="Test", total_files=1)
        tp.add_file(0, "video.mp4", total_bytes=1024)
        tp.mark_downloading(0, total_bytes=2048)
        fp = tp._get(0)
        assert fp.status == FileStatus.DOWNLOADING
        assert fp.total_bytes == 2048

    def test_update_download(self):
        tp = TaskProgress(title="Test", total_files=1)
        tp.add_file(0, "video.mp4", total_bytes=1024)
        tp.mark_downloading(0, total_bytes=1024)
        tp.update_download(0, current_bytes=512)
        fp = tp._get(0)
        assert fp.current_bytes == 512
        assert fp.percent == 50.0

    def test_mark_uploading(self):
        tp = TaskProgress(title="Test", total_files=1)
        tp.add_file(0, "video.mp4", total_bytes=1024)
        tp.mark_uploading(0)
        fp = tp._get(0)
        assert fp.status == FileStatus.UPLOADING

    def test_mark_sent(self):
        tp = TaskProgress(title="Test", total_files=1)
        tp.add_file(0, "video.mp4", total_bytes=1024)
        tp.mark_sent(0)
        fp = tp._get(0)
        assert fp.status == FileStatus.SENT

    def test_mark_failed(self):
        tp = TaskProgress(title="Test", total_files=1)
        tp.add_file(0, "video.mp4", total_bytes=1024)
        tp.mark_failed(0, error="Network timeout")
        fp = tp._get(0)
        assert fp.status == FileStatus.FAILED
        assert fp.error == "Network timeout"

    def test_mark_skipped(self):
        tp = TaskProgress(title="Test", total_files=1)
        tp.add_file(0, "video.mp4", total_bytes=1024)
        tp.mark_skipped(0)
        fp = tp._get(0)
        assert fp.status == FileStatus.SKIPPED

    def test_has_failures(self):
        tp = TaskProgress(title="Test", total_files=2)
        tp.add_file(0, "ok.mp4", total_bytes=1024)
        tp.add_file(1, "bad.mp4", total_bytes=1024)
        assert tp.has_failures is False
        tp.mark_failed(1, error="err")
        assert tp.has_failures is True

    def test_sent_count(self):
        tp = TaskProgress(title="Test", total_files=3)
        tp.add_file(0, "a.mp4", total_bytes=100)
        tp.add_file(1, "b.mp4", total_bytes=100)
        tp.add_file(2, "c.mp4", total_bytes=100)
        assert tp.sent_count == 0
        tp.mark_sent(0)
        tp.mark_sent(2)
        assert tp.sent_count == 2

    def test_build_message_pending(self):
        tp = TaskProgress(title="Downloading 2 files", total_files=2)
        tp.add_file(0, "a.mp4", total_bytes=100)
        tp.add_file(1, "b.mp4", total_bytes=100)
        msg = tp.build_message()
        assert "Downloading 2 files" in msg

    def test_build_message_with_sent(self):
        tp = TaskProgress(title="Download", total_files=2)
        tp.add_file(0, "a.mp4", total_bytes=100)
        tp.add_file(1, "b.mp4", total_bytes=100)
        tp.mark_sent(0)
        msg = tp.build_message()
        assert "a.mp4" in msg

    def test_get_error_summary_no_errors(self):
        tp = TaskProgress(title="Test", total_files=1)
        tp.add_file(0, "ok.mp4", total_bytes=100)
        tp.mark_sent(0)
        summary = tp.get_error_summary()
        assert summary == ""

    def test_get_error_summary_with_errors(self):
        tp = TaskProgress(title="Test", total_files=1)
        tp.add_file(0, "bad.mp4", total_bytes=100)
        tp.mark_failed(0, error="Download failed")
        summary = tp.get_error_summary()
        assert "Download failed" in summary
        assert "bad.mp4" in summary

    def test_get_nonexistent_file_raises(self):
        tp = TaskProgress(title="Test", total_files=1)
        tp.add_file(0, "a.mp4", total_bytes=100)
        with pytest.raises(IndexError):
            tp._get(99)
