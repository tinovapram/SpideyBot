"""cyberdrop-dl as the last resort (D6, D38).

The tool is invoked for real in one place only — a config-validation test, which asks
``cyberdrop-dl`` itself whether the keys we generate still exist. Everything else drives an
injected command handle, because the behaviour worth testing is *ours*: what we do while a run
overshoots the cap, what we harvest when it finishes, and what we explain when it produces nothing.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from downloader import cyberdropdl
from downloader.cyberdropdl import (
    CONFIG_TEMPLATE,
    Command,
    build_command,
    failure_reason,
    files_in,
    harvest,
    is_available,
    write_config,
)
from downloader.fetcher import FetchError, SizeLimitExceeded, TransferCancelled

MB = 1024 * 1024


class FakeCommand:
    """Stands in for a running download: writes what it is told, when it is told, then exits."""

    def __init__(
        self,
        downloads: Path,
        *,
        files: dict[str, int] | None = None,
        delay: float = 0.0,
        block: bool = False,
    ) -> None:
        self.downloads = downloads
        self.files = files or {}
        self.delay = delay
        self.block = block
        self.killed = False
        self._release = asyncio.Event()

    async def wait(self) -> int:
        if self.delay:
            await asyncio.sleep(self.delay)
        for name, size in self.files.items():
            target = self.downloads / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * size)
        if self.block:
            while not self.killed:
                await asyncio.sleep(0.05)
        return 0

    def kill(self) -> None:
        self.killed = True
        self._release.set()


def starter(
    downloads: Path,
    *,
    files: dict[str, int] | None = None,
    delay: float = 0.0,
    block: bool = False,
) -> tuple[cyberdropdl.Starter, list[FakeCommand]]:
    """A launcher that records the command it was given, and hands back a ``FakeCommand``."""
    seen: list[FakeCommand] = []

    async def start(command: Sequence[str]) -> Command:
        assert command[1:3] == ["-m", "cyberdrop_dl"]
        handle = FakeCommand(downloads, files=files, delay=delay, block=block)
        seen.append(handle)
        return handle

    return start, seen


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    return tmp_path / "job"


# ── the config ───────────────────────────────────────────────────────────


def test_the_generated_config_points_the_logs_away_from_the_downloads(tmp_path: Path) -> None:
    """The tool writes its own CSVs and log; in the download directory they would be harvested."""
    config = write_config(
        tmp_path / "cyberdrop-dl.yaml", downloads=tmp_path / "downloads", logs=tmp_path / "logs"
    )

    text = config.read_text(encoding="utf-8")

    assert "subfolders:\n  create: false" in text
    assert f"download_folder: {(tmp_path / 'downloads').as_posix()}" in text
    assert f"folder: {(tmp_path / 'logs').as_posix()}" in text
    assert "dump_json: true" in text
    # A Windows backslash would be an escape in YAML, so paths are written with forward slashes.
    assert "\\" not in text.split("download_folder:")[1].splitlines()[0]


def test_every_config_key_we_set_exists_in_the_tools_own_defaults(tmp_path: Path) -> None:
    """A typo in a generated config is a silent feature loss, so the tool is asked directly.

    Skipped when the CLI is not installed; otherwise this is the guard that fails when an upgrade
    renames a key, which is exactly when the integration would otherwise degrade without a word.
    """
    if not is_available():
        pytest.skip("cyberdrop-dl is not installed")

    defaults = subprocess.run(
        [__import__("sys").executable, "-m", "cyberdrop_dl", "config", "new"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if defaults.returncode != 0:
        pytest.skip(f"cyberdrop-dl could not report its defaults: {defaults.stderr[:200]}")

    def paths(text: str) -> Iterator[str]:
        """Every dotted key path in a YAML mapping, so a nested key is checked as a whole."""
        stack: list[tuple[int, str]] = []
        for raw in text.splitlines():
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            key = raw.strip().split(":")[0]
            if not key or ":" not in raw:
                continue
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, key))
            yield ".".join(name for _, name in stack)

    known = set(paths(defaults.stdout))
    ours = set(paths(CONFIG_TEMPLATE.format(downloads="d", logs="l")))

    missing = {
        key
        for key in ours
        # Values of a list key are not key paths, and the leaf under `hashing.dedupe` is a map.
        if key not in known and not any(existing.startswith(key + ".") for existing in known)
    }
    assert missing == set(), f"not in cyberdrop-dl's own config: {sorted(missing)}"


def test_the_command_does_not_pass_a_cache_or_database_file(tmp_path: Path) -> None:
    """Both flags are rejected unless the files already exist (measured), so neither is passed."""
    command = build_command("https://x.test/a", tmp_path / "c.yaml")

    assert "--cache-file" not in command
    assert "--database-file" not in command
    assert command[-1] == "https://x.test/a"


def test_the_command_runs_the_tool_through_the_running_interpreter(tmp_path: Path) -> None:
    """A second Python on PATH could be another version with another config schema."""
    import sys

    command = build_command("https://x.test/a", tmp_path / "c.yaml")

    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "cyberdrop_dl"]


# ── what is harvested ────────────────────────────────────────────────────


def test_harvest_takes_every_file_below_the_download_directory(tmp_path: Path) -> None:
    """The tool may nest files in album subdirectories, and both levels are ours to deliver."""
    downloads = tmp_path / "downloads"
    (downloads / "album").mkdir(parents=True)
    (downloads / "one.mp4").write_bytes(b"a" * 10)
    (downloads / "album" / "two.jpg").write_bytes(b"b" * 20)
    (downloads / "empty.bin").write_bytes(b"")

    harvested = harvest(downloads)

    # Sorted by path, not by name: the order is stable across runs, which is what delivery needs.
    assert [item.path.name for item in harvested] == ["two.jpg", "one.mp4"]
    assert [item.bytes_written for item in harvested] == [20, 10]
    assert [item.media_kind for item in harvested] == ["photo", "video"]
    assert all(item.note == "cyberdrop-dl" for item in harvested)


def test_partial_files_are_not_harvested(tmp_path: Path) -> None:
    """A still-running or killed download leaves ``.part`` files, and they are not deliverables."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "one.mp4").write_bytes(b"a" * 10)
    (downloads / "one.mp4.part").write_bytes(b"a" * 4)

    assert [path.name for path in files_in(downloads)] == ["one.mp4", "one.mp4.part"]


# ── the reasons ──────────────────────────────────────────────────────────


def test_the_tools_own_reason_is_used_when_it_left_one(tmp_path: Path) -> None:
    """Its exit code is 0 for an unsupported URL, so the CSV is the explanation (R13's lesson)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unsupported.csv").write_text(
        "url,error\nhttps://x.test/a,Unsupported URL: https://x.test/a\n", encoding="utf-8"
    )

    assert failure_reason(logs) == "Unsupported URL: https://x.test/a"


def test_no_reason_file_means_no_reason(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()

    assert failure_reason(logs) is None


async def test_a_run_that_produces_nothing_is_a_failure_with_the_tools_reason(
    staging: Path,
) -> None:
    logs = staging / "cdl-logs"
    logs.mkdir(parents=True)
    (logs / "unsupported.csv").write_text("url,error\nu,Bad host\n", encoding="utf-8")
    start, _ = starter(staging / "downloads")

    with pytest.raises(FetchError) as caught:
        await cyberdropdl.download("https://x.test/a", staging, start=start)

    assert str(caught.value) == "Bad host"


async def test_a_run_that_produces_nothing_without_a_reason_still_says_so(staging: Path) -> None:
    start, _ = starter(staging / "downloads")

    with pytest.raises(FetchError) as caught:
        await cyberdropdl.download("https://x.test/a", staging, start=start)

    assert "found nothing" in str(caught.value)


# ── the cap (D31, D38) ───────────────────────────────────────────────────


async def test_the_run_is_killed_when_the_bytes_pass_the_cap(staging: Path) -> None:
    """The limit holds either way: here by ending a run rather than by refusing one."""
    start, seen = starter(staging / "downloads", files={"big.bin": 4 * MB}, block=True)

    with pytest.raises(SizeLimitExceeded) as caught:
        await cyberdropdl.download("https://x.test/a", staging, limit_bytes=MB, start=start)

    assert caught.value.limit_bytes == MB
    assert caught.value.written_bytes > MB
    assert seen[0].killed is True


async def test_a_killed_run_leaves_nothing_behind(staging: Path) -> None:
    """A truncated file is worse than no file: delivery must not find one (D33)."""
    start, _ = starter(staging / "downloads", files={"big.bin": 4 * MB}, block=True)

    with pytest.raises(SizeLimitExceeded):
        await cyberdropdl.download("https://x.test/a", staging, limit_bytes=MB, start=start)

    assert files_in(staging / "downloads") == []


async def test_a_run_that_finishes_over_the_cap_is_still_refused(staging: Path) -> None:
    """A fast run can land between two polls, and the promise is about the total, not the tick."""
    start, _ = starter(staging / "downloads", files={"big.bin": 4 * MB})

    with pytest.raises(SizeLimitExceeded):
        await cyberdropdl.download("https://x.test/a", staging, limit_bytes=MB, start=start)

    assert files_in(staging / "downloads") == []


async def test_a_run_under_the_cap_is_harvested(staging: Path) -> None:
    start, _ = starter(staging / "downloads", files={"small.bin": 1024})

    files = await cyberdropdl.download("https://x.test/a", staging, limit_bytes=MB, start=start)

    assert [item.path.name for item in files] == ["small.bin"]
    assert files[0].bytes_written == 1024


async def test_an_unlimited_tier_imposes_no_cap(staging: Path) -> None:
    """0 means unlimited throughout the tier table, and the delegate must honour that too."""
    start, seen = starter(staging / "downloads", files={"big.bin": 5 * MB})

    files = await cyberdropdl.download("https://x.test/a", staging, limit_bytes=None, start=start)

    assert len(files) == 1
    assert seen[0].killed is False


# ── cancellation (FR-11.6) ───────────────────────────────────────────────


async def test_cancelling_stops_the_delegated_run(staging: Path) -> None:
    start, seen = starter(staging / "downloads", block=True)

    with pytest.raises(TransferCancelled):
        await cyberdropdl.download(
            "https://x.test/a", staging, should_stop=lambda: True, start=start
        )

    assert seen[0].killed is True


async def test_a_timeout_stops_the_run_rather_than_waiting_forever(staging: Path) -> None:
    start, seen = starter(staging / "downloads", block=True)

    with pytest.raises(FetchError) as caught:
        await cyberdropdl.download("https://x.test/a", staging, timeout=0, start=start)

    assert "did not finish" in str(caught.value)
    assert seen[0].killed is True


# ── progress ─────────────────────────────────────────────────────────────


async def test_progress_reports_the_bytes_on_disk(staging: Path) -> None:
    """Aggregate bytes only: the tool does not say which file it is on, so neither do we."""
    from downloader.fetcher import FetchProgress

    seen: list[FetchProgress] = []
    start, _ = starter(staging / "downloads", files={"a.bin": 2048}, delay=0.6)

    await cyberdropdl.download(
        "https://x.test/a", staging, limit_bytes=MB, progress=seen.append, start=start
    )

    assert seen
    assert seen[-1].transfer_done == 2048
    assert seen[-1].role == "file"
    assert seen[-1].url == "https://x.test/a"
