"""cyberdrop-dl as the last resort (D6, D38).

Every other resolver hands back transfers that :mod:`downloader.fetcher` performs. This one cannot:
cyberdrop-dl is a program that downloads, so the last resort *delegates* the transfer and we harvest
what appears on disk. That surrenders one thing and keeps another:

* **Surrendered: the pre-transfer size gate.** A CLI cannot say what it is about to fetch, so AC-3's
  measurement is unavailable and D31's fallback is reused instead — the run is watched and stopped
  the moment the bytes on disk pass the tier's cap. The limit still holds; it holds by ending a
  download rather than by refusing one. That is a real cost, which is why this path runs last, after
  every measured one.
* **Kept: the failure reason.** The CLI records unsupported URLs and download errors as CSVs beside
  its logs, so "nothing arrived" can be explained rather than guessed. Its exit code cannot explain
  anything: it exits **0** for an unsupported URL (measured), exactly as gallery-dl exits 0 on a 404
  (R13). The files on disk are the only evidence of success.

The config is generated per run and its keys are checked against the tool's own defaults in the
tests, because a typo in a generated config is a silent feature loss: the run still succeeds, just
not the way this module means. Two settings matter for correctness rather than taste —
``subfolders.create: false`` so files land where the harvest looks, and ``logs.folder`` pointed
*away* from the download directory, since the CLI also writes ``unsupported.csv`` and
``downloader.log`` and neither of those is a downloaded file.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import importlib.util
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol

from downloader.contracts import kind_for
from downloader.fetcher import (
    DEFAULT_TIMEOUT,
    FetchedFile,
    FetchError,
    FetchProgress,
    ProgressHook,
    SizeLimitExceeded,
    TransferCancelled,
)

log = logging.getLogger(__name__)

#: The module rather than a bare ``cyberdrop-dl``, so the interpreter already running the bot runs
#: the tool too: a second Python on PATH could be another version with another config schema.
EXECUTABLE = ("-m", "cyberdrop_dl")

#: How often a running download is measured against the cap. Often enough that the overshoot is a
#: rounding error, rare enough to be nothing like a hot loop.
POLL_INTERVAL = 0.5

#: The subset of the tool's config this integration depends on, each key checked against the tool's
#: own defaults in the tests.
CONFIG_TEMPLATE = """\
download_folder: {downloads}
dump_json: true
delete_partial_files: true
ignore_history: true
ignore_hashes: true
hashing:
  mode: "off"
  dedupe:
    enabled: false
    use_trash_bin: false
mtime: false
max_thread_depth: 0
sort:
  enabled: false
subfolders:
  create: false
ui:
  mode: disabled
  show_stats: false
update_check: false
logs:
  level: WARNING
  http_traffic: false
  folder: {logs}
"""

#: Written by the CLI when it cannot handle a URL or a file. Read for the reason, never for success.
REASON_FILES = ("unsupported.csv", "download_errors.csv", "scrape_errors.csv")


class Command(Protocol):
    """A running external command: enough to wait for it, and enough to stop it.

    ``kill`` is the reason this is a handle rather than a coroutine returning an exit code —
    cancelling the awaiting task would leave the real process running, which for a download means
    bandwidth spent after the user was told it stopped.
    """

    async def wait(self) -> int: ...

    def kill(self) -> None: ...


Starter = Callable[[Sequence[str]], Awaitable[Command]]


def is_available() -> bool:
    """Whether the tool is installed, so the caller can decide about its last resort (D6)."""
    return importlib.util.find_spec("cyberdrop_dl") is not None


class _ProcessCommand:
    """The real thing: an ``asyncio`` subprocess, killed for real when asked."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def wait(self) -> int:
        _, stderr = await self._process.communicate()
        code = self._process.returncode or 0
        if code != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
            log.info("cyberdrop-dl exited %s: %s", code, (detail[-1] if detail else "")[:200])
        return code

    def kill(self) -> None:
        try:
            self._process.kill()
        except ProcessLookupError:  # pragma: no cover - already gone, which is the goal
            pass


async def _start(command: Sequence[str]) -> Command:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return _ProcessCommand(process)


def write_config(config_path: Path, *, downloads: Path, logs: Path) -> Path:
    """Write this run's config.

    Paths go in with forward slashes: the YAML is the tool's, and a Windows backslash would be read
    as an escape.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        CONFIG_TEMPLATE.format(downloads=downloads.as_posix(), logs=logs.as_posix()),
        encoding="utf-8",
    )
    return config_path


def build_command(url: str, config_path: Path, *, executable: str | None = None) -> list[str]:
    """The invocation for one link.

    Neither ``--cache-file`` nor ``--database-file`` is passed: the tool rejects both unless the
    files already exist (measured), and its own defaults are fine because history is ignored anyway.
    """
    return [
        executable or sys.executable,
        *EXECUTABLE,
        "download",
        "--config-file",
        str(config_path),
        url,
    ]


def files_in(directory: Path) -> list[Path]:
    """Every non-empty file under ``directory``.

    ``.part`` files are excluded by name: a killed or *still running* download leaves them, and the
    tool's own ``delete_partial_files`` only applies when it exits cleanly.
    """
    if not directory.exists():
        return []
    return [
        path
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.stat().st_size > 0 and not path.name.startswith(".")
    ]


def harvest(downloads: Path) -> tuple[FetchedFile, ...]:
    """What the run produced, as the same type the fetcher returns.

    Sizes come from the filesystem because the filesystem is what delivery will read, and the order
    is stable so two runs of one link deliver the same sequence.
    """
    return tuple(
        FetchedFile(
            path=path,
            media_kind=kind_for(path.suffix),
            bytes_written=path.stat().st_size,
            note="cyberdrop-dl",
        )
        for path in files_in(downloads)
    )


def failure_reason(logs: Path) -> str | None:
    """The tool's own explanation, when it left one."""
    for name in REASON_FILES:
        path = logs / name
        if not path.is_file():
            continue
        try:
            with path.open(newline="", encoding="utf-8", errors="replace") as handle:
                rows = list(csv.DictReader(handle))
        except OSError:  # pragma: no cover - unreadable bookkeeping is not worth failing over
            continue
        for row in rows:
            for key in ("error", "message", "reason", "description"):
                value = (row.get(key) or "").strip()
                if value:
                    return value[:300]
    return None


async def download(
    url: str,
    staging: Path,
    *,
    limit_bytes: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    progress: ProgressHook | None = None,
    should_stop: Callable[[], bool] | None = None,
    start: Starter | None = None,
) -> tuple[FetchedFile, ...]:
    """Let cyberdrop-dl fetch ``url`` into ``staging``, watching the cap while it runs.

    Raises :class:`SizeLimitExceeded` when the bytes on disk pass the cap,
    :class:`TransferCancelled` when the user cancels, and :class:`FetchError` when the run ends with
    nothing to show.
    """
    downloads = staging / "downloads"
    logs = staging / "cdl-logs"
    downloads.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    config_path = write_config(staging / "cyberdrop-dl.yaml", downloads=downloads, logs=logs)
    command = build_command(url, config_path)
    launcher = start or _start
    handle = await launcher(command)

    # One waiter task for the whole run: awaiting the handle itself in each poll would try to
    # communicate with the same process more than once, which asyncio forbids.
    waiter = asyncio.ensure_future(handle.wait())
    stopped: str | None = None
    written = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=POLL_INTERVAL)
            if done:
                written = _measure(downloads)
                break

            written = _measure(downloads)
            if progress is not None:
                await _report(progress, url, written, limit_bytes)
            if limit_bytes is not None and written > limit_bytes:
                stopped = "cap"
            elif should_stop is not None and should_stop():
                stopped = "stop"
            elif time.monotonic() > deadline:
                stopped = "timeout"
            if stopped is not None:
                break

        if stopped is not None:
            handle.kill()
            await _reap(waiter)
            # Measured after the kill, so the reported total is what really came off the network.
            written = _measure(downloads)
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

    if stopped is not None:
        # A killed run leaves partial files in the caller's staging area. They are removed here
        # rather than passed on, because a truncated file is worse than no file (D33).
        _discard_all(downloads)
        if stopped == "cap":
            assert limit_bytes is not None
            raise SizeLimitExceeded(limit_bytes, written)
        if stopped == "stop":
            raise TransferCancelled()
        raise FetchError(f"cyberdrop-dl did not finish within {timeout:.0f}s")

    if limit_bytes is not None and written > limit_bytes:
        # A fast run can land between two polls; the promise is about the total, not the tick.
        _discard_all(downloads)
        raise SizeLimitExceeded(limit_bytes, written)

    files = harvest(downloads)
    if not files:
        raise FetchError(failure_reason(logs) or "cyberdrop-dl found nothing at that link")
    if progress is not None:
        await _report(progress, url, written, limit_bytes)
    log.info("cyberdrop-dl produced %d file(s) from %s", len(files), url)
    return files


def _measure(downloads: Path) -> int:
    return sum(path.stat().st_size for path in files_in(downloads))


async def _reap(waiter: asyncio.Future[int]) -> None:
    """Wait for a killed command so its resources are released before the job continues."""
    try:
        await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
    except TimeoutError:  # pragma: no cover - a stalled kill
        log.warning("cyberdrop-dl did not exit after being stopped")
    except asyncio.CancelledError:  # pragma: no cover - the job itself is going away
        raise
    except Exception:  # noqa: BLE001 - a dead process's error is not this job's failure
        log.debug("cyberdrop-dl exited badly after being stopped", exc_info=True)


def _discard_all(downloads: Path) -> None:
    for path in files_in(downloads):
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - a locked file on Windows; the caller reports anyway
            log.warning("could not remove the partial file %s", path)


async def _report(progress: ProgressHook, url: str, written: int, limit_bytes: int | None) -> None:
    update = FetchProgress(
        url=url,
        role="file",
        transfer_done=written,
        transfer_total=limit_bytes,
        overall_done=written,
        overall_total=limit_bytes,
    )
    result = progress(update)
    if isinstance(result, Awaitable):
        await result
