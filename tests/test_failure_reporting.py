from downloader.flow import _failed_files_message as generic_failed_files_message
from downloader.terabox_flow import _failed_files_message as terabox_failed_files_message


def test_generic_failed_files_include_source_links() -> None:
    text = generic_failed_files_message(
        [("clip [final].mp4", "upload failed")],
        "https://example.test/share?id=42",
    )

    assert "[clip \\[final\\].mp4](https://example.test/share?id=42)" in text
    assert "upload failed" in text


def test_terabox_failed_files_include_source_links() -> None:
    text = terabox_failed_files_message(
        [("movie.mp4", "empty file")],
        "https://example.test/terabox-share",
    )

    assert "[movie.mp4](https://example.test/terabox-share)" in text
    assert "empty file" in text
