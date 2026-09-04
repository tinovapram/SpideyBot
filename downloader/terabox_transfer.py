"""
Multi-backend transfer engine for TeraBox direct file links.

Why this exists
---------------
TeraBox/Baidu CDNs throttle a *single* TCP connection over time: downloads
start fast and progressively slow down, which is very noticeable on large
files. The original code streamed each file over exactly one connection, so
there was no way to recover when the server throttled that connection.

This module provides two throttling-resistant strategies plus the original
single-stream path as a safe fallback:

* ``aria2_download``
    Delegates to the ``aria2c`` binary: parallel Range connections
    (``-x``/``-s``), automatic retry and on-disk resume (``-c``). Best option
    for large files and works for both account and direct share links.
* ``segmented_download``
    Native asyncio download split into ``connections`` parallel Range
    requests, with per-segment stall detection that reconnects from the
    current byte offset. Used for account links when aria2 is unavailable.
* ``single_stream_download``
    Original behaviour (one connection), kept as the fallback and extended
    with stall detection + reconnect/resume so it no longer dies silently.

Backend selection is controlled by ``TERABOX_TRANSFER``
(``auto|aria2|segmented|single``) and the size threshold in ``core.config``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from typing import Callable, Optional

import aiohttp
import structlog

from core import config

# Progress callback signature used across TeraBox downloaders:
# cb(filename: str, done_bytes: int, total_bytes: int)
ProgressCallback = Optional[Callable[[str, int, int], None]]

_CHUNK = 256 * 1024  # 256 KiB native read chunk
_MERGE_CHUNK = 1024 * 1024
_MIN_PART_BYTES = 1 * 1024 * 1024  # never create segments smaller than this

_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_PCT_RE = re.compile(r"\((\d{1,3})%\)")
_BYTE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT])i?B")
_CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+)")


class TransferError(Exception):
    """Raised when a transfer backend cannot complete a download."""


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("terabox_transfer")


def aria2_available() -> bool:
    """Return True when the ``aria2c`` binary is on PATH."""
    return shutil.which("aria2c") is not None


def pick_transfer_backend(size_bytes: int) -> str:
    """Choose ``aria2``, ``segmented`` or ``single`` for a file of *size_bytes*.

    Honours ``TERABOX_TRANSFER``; ``auto`` uses aria2 when present and the
    file is big enough, otherwise native segmented, otherwise single-stream.
    """
    mode = (config.TERABOX_TRANSFER or "auto").strip().lower()
    if mode in ("aria2", "segmented", "single"):
        return mode

    if size_bytes >= config.TERABOX_TRANSFER_MIN_BYTES:
        if aria2_available():
            return "aria2"
        return "segmented"
    return "single"


def wipe_partial(filepath: str) -> None:
    """Remove a partial target, its aria2 control file and any ``.part*`` files."""
    for path in (filepath, filepath + ".aria2"):
        try:
            os.remove(path)
        except OSError:
            pass
    dirpath = os.path.dirname(filepath)
    prefix = os.path.basename(filepath) + ".part"
    if os.path.isdir(dirpath):
        for name in os.listdir(dirpath):
            if name.startswith(prefix):
                try:
                    os.remove(os.path.join(dirpath, name))
                except OSError:
                    pass


def _format_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _parse_readout(line: str, expected_size: int) -> Optional[int]:
    """Extract downloaded bytes from an aria2 console readout line."""
    if not line:
        return None
    pct = _PCT_RE.search(line)
    if pct and expected_size > 0:
        value = int(pct.group(1))
        if 0 <= value <= 100:
            return min(expected_size, int(expected_size * value / 100))
    matches = _BYTE_RE.findall(line)
    if len(matches) >= 2:
        def _to_bytes(pair) -> float:
            return float(pair[0]) * _UNITS[pair[1].upper()]
        done, total = _to_bytes(matches[0]), _to_bytes(matches[1])
        if total > 0:
            return min(int(done), int(total))
    return None


# ════════════════════════════════════════════════════════════════════
# aria2c backend
# ════════════════════════════════════════════════════════════════════

async def aria2_download(
    url: str,
    filepath: str,
    *,
    headers: Optional[dict] = None,
    expected_size: int = 0,
    progress_callback: ProgressCallback = None,
    connections: int = 16,
    stall_timeout: int = 300,
    logger=None,
) -> str:
    """Download *url* to *filepath* by shelling out to ``aria2c``.

    aria2 opens up to *connections* parallel Range connections, retries failed
    segments itself and resumes from disk (``-c``). Progress is recovered by
    parsing aria2's periodic console readout (falling back to on-disk size).
    """
    log = logger or _logger()
    if not aria2_available():
        raise TransferError("aria2c is not installed/available on PATH")

    dirpath = os.path.dirname(filepath) or "."
    filename = os.path.basename(filepath)
    os.makedirs(dirpath, exist_ok=True)
    wipe_partial(filepath)

    cmd = [
        "aria2c",
        f"--dir={dirpath}",
        f"--out={filename}",
        f"-x{connections}",
        f"-s{connections}",
        "-k1M",
        "--min-split-size=4M",
        "-c",  # continue/resume partial file + control file
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--file-allocation=none",
        "--max-tries=5",
        "--retry-wait=1",
        "--timeout=30",
        "--connect-timeout=30",
        "--summary-interval=1",
        "--console-log-level=error",
        "--download-result=hide",
        "--no-netrc=true",
    ]
    for key, value in (headers or {}).items():
        cmd.append(f"--header={key}: {value}")
    cmd.append(url)

    log.info("aria2 download start", file=filename, size=_format_bytes(expected_size),
             connections=connections)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TransferError("aria2c could not be started") from exc

    fname = filename
    target = filepath
    last_growth = time.monotonic()
    last_done = 0
    reported = 0

    async def _reader(stream, queue):
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                queue.put_nowait(line)
        except Exception:
            pass

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    readers = [
        asyncio.create_task(_reader(proc.stdout, queue)),
        asyncio.create_task(_reader(proc.stderr, queue)),
    ]

    try:
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                line = None

            done = 0
            if line is not None:
                parsed = _parse_readout(line.decode("utf-8", "replace"), expected_size)
                if parsed is not None:
                    done = parsed
                else:
                    # fall back to how much of the target exists on disk
                    done = os.path.getsize(target) if os.path.exists(target) else 0
                if done > last_done:
                    last_growth = time.monotonic()
                    last_done = done
            else:
                done = os.path.getsize(target) if os.path.exists(target) else 0

            if progress_callback and done != reported:
                progress_callback(fname, min(done, expected_size or done), expected_size or done)
                reported = done

            if proc.returncode is not None:
                break

            if (time.monotonic() - last_growth) > stall_timeout:
                proc.terminate()
                raise TransferError(
                    f"aria2 stalled: no progress for {stall_timeout}s on {fname}"
                )

        await proc.wait()
        for task in readers:
            task.cancel()

        if proc.returncode != 0:
            raise TransferError(f"aria2c exited with code {proc.returncode}")

        if not os.path.exists(target) or os.path.getsize(target) == 0:
            raise TransferError(f"aria2c produced an empty file for {fname}")

        final = os.path.getsize(target)
        if expected_size > 0 and final != expected_size:
            log.warning("aria2 size mismatch", file=fname,
                        expected=expected_size, actual=final)
        if progress_callback:
            progress_callback(fname, final, expected_size or final)
        log.info("aria2 download done", file=fname, size=_format_bytes(final))
        return filepath
    except Exception:
        for task in readers:
            task.cancel()
        try:
            if proc.returncode is None:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        raise


# ════════════════════════════════════════════════════════════════════
# Native segmented backend (asyncio + Range)
# ════════════════════════════════════════════════════════════════════

async def _probe_range(
    session: aiohttp.ClientSession, url: str, headers: dict, stall_timeout: int
) -> Optional[int]:
    """Return total content length when the server honours Range, else None."""
    try:
        timeout = aiohttp.ClientTimeout(total=0, connect=30, sock_read=stall_timeout)
        response = await session.get(
            url, headers={**headers, "Range": "bytes=0-0"}, timeout=timeout
        )
        try:
            if response.status != 206:
                return None
            content_range = response.headers.get("Content-Range", "")
            match = _CONTENT_RANGE_RE.search(content_range)
            return int(match.group(1)) if match else None
        finally:
            await response.release()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def _download_segment(
    session: aiohttp.ClientSession,
    url: str,
    part_path: str,
    start: int,
    end: int,
    *,
    headers: dict,
    stall_timeout: int,
    retries: int,
    chunk_size: int,
    logger,
) -> None:
    """Download ``[start, end]`` into *part_path*, reconnecting on stall."""
    written = 0
    total = end - start + 1
    for attempt in range(1 + retries):
        resume_at = start + written
        if resume_at > end:
            break
        range_header = f"bytes={resume_at}-{end}"
        timeout = aiohttp.ClientTimeout(total=0, connect=30, sock_read=stall_timeout)
        try:
            response = await session.get(
                url,
                headers={**headers, "Range": range_header},
                timeout=timeout,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            raise TransferError(f"segment request failed at {resume_at}: {exc}") from exc

        try:
            if response.status != 206:
                raise TransferError(
                    f"server did not honour Range (HTTP {response.status}) at {resume_at}"
                )
            mode = "ab" if written else "wb"
            with open(part_path, mode) as handle:
                iterator = response.content.iter_chunked(chunk_size)
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(), timeout=stall_timeout
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError as exc:
                        raise TransferError(
                            f"segment stalled at {resume_at + written}"
                        ) from exc
                    if chunk:
                        handle.write(chunk)
                        written += len(chunk)
            if written >= total:
                return
            if attempt < retries:
                continue  # short body -> reconnect from current offset
            raise TransferError(
                f"segment ended early ({written}/{total} bytes)"
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, TransferError) as exc:
            if isinstance(exc, TransferError) and "did not honour" in str(exc):
                raise
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            raise TransferError(f"segment {start}-{end} failed: {exc}") from exc
        finally:
            await response.release()
    raise TransferError(f"segment {start}-{end} gave up after retries")


async def segmented_download(
    url: str,
    filepath: str,
    *,
    session: Optional[aiohttp.ClientSession] = None,
    headers: Optional[dict] = None,
    expected_size: int = 0,
    progress_callback: ProgressCallback = None,
    connections: int = 8,
    stall_timeout: int = 300,
    retries: int = 3,
    logger=None,
) -> str:
    """Native parallel Range download into *filepath* using *session*."""
    log = logger or _logger()
    headers = dict(headers or {})
    dirpath = os.path.dirname(filepath) or "."
    filename = os.path.basename(filepath)
    os.makedirs(dirpath, exist_ok=True)
    wipe_partial(filepath)

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=0, connect=30, sock_read=stall_timeout),
        )
    try:
        total = await _probe_range(session, url, headers, stall_timeout) or expected_size
        if total <= 0:
            raise TransferError("cannot determine file size / server ignores Range")

        parts = max(1, min(connections, total // _MIN_PART_BYTES))
        if parts < 1:
            parts = 1
        part_len = (total + parts - 1) // parts

        part_files = [f"{filepath}.part{i:02d}" for i in range(parts)]
        log.info("segmented download start", file=filename,
                 size=_format_bytes(total), parts=parts)

        async def _run(i: int) -> None:
            start = i * part_len
            end = min(start + part_len - 1, total - 1)
            if start > end:
                return
            await _download_segment(
                session, url, part_files[i], start, end,
                headers=headers, stall_timeout=stall_timeout,
                retries=retries, chunk_size=_CHUNK, logger=log,
            )

        # A lightweight notifier so Telegram progress stays live.
        stop = asyncio.Event()

        async def _notifier() -> None:
            last = -1
            while not stop.is_set():
                await asyncio.sleep(0.5)
                done = sum(
                    os.path.getsize(p) if os.path.exists(p) else 0 for p in part_files
                )
                done = min(done, total)
                if done != last:
                    last = done
                    if progress_callback:
                        progress_callback(filename, done, total)

        notifier = asyncio.create_task(_notifier())
        try:
            await asyncio.gather(*(_run(i) for i in range(parts)))
        finally:
            stop.set()
            notifier.cancel()

        # Merge parts, in order, into the final file.
        with open(filepath, "wb") as final:
            for part_path in part_files:
                if not os.path.exists(part_path):
                    raise TransferError(f"missing part {part_path}")
                with open(part_path, "rb") as part:
                    while True:
                        buf = part.read(_MERGE_CHUNK)
                        if not buf:
                            break
                        final.write(buf)

        for part_path in part_files:
            try:
                os.remove(part_path)
            except OSError:
                pass

        final_size = os.path.getsize(filepath)
        if progress_callback:
            progress_callback(filename, final_size, total)
        if final_size != total:
            raise TransferError(
                f"merged size {final_size} != expected {total}"
            )
        log.info("segmented download done", file=filename, size=_format_bytes(final_size))
        return filepath
    finally:
        if own_session:
            await session.close()


# ════════════════════════════════════════════════════════════════════
# Single-stream backend (original behaviour, resume-capable)
# ════════════════════════════════════════════════════════════════════

async def single_stream_download(
    url: str,
    filepath: str,
    *,
    session: Optional[aiohttp.ClientSession] = None,
    headers: Optional[dict] = None,
    expected_size: int = 0,
    progress_callback: ProgressCallback = None,
    stall_timeout: int = 300,
    retries: int = 3,
    logger=None,
) -> str:
    """Download *url* to *filepath* over one connection with stall resume."""
    log = logger or _logger()
    headers = dict(headers or {})
    dirpath = os.path.dirname(filepath) or "."
    filename = os.path.basename(filepath)
    os.makedirs(dirpath, exist_ok=True)
    wipe_partial(filepath)

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers=headers)

    try:
        done = 0
        final_exc: Optional[Exception] = None
        for attempt in range(1 + retries):
            req_headers = dict(headers)
            if done:
                req_headers["Range"] = f"bytes={done}-"
            timeout = aiohttp.ClientTimeout(total=0, connect=30, sock_read=stall_timeout)
            try:
                response = await session.get(url, headers=req_headers, timeout=timeout)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                final_exc = exc
                if attempt < retries:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                break

            try:
                if response.status not in (200, 206):
                    raise TransferError(f"HTTP {response.status} for {filename}")
                if done and response.status == 200:
                    # Server ignored our Range: restart cleanly.
                    done = 0
                with open(filepath, "ab" if done else "wb") as handle:
                    iterator = response.content.iter_chunked(_CHUNK)
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                iterator.__anext__(), timeout=stall_timeout
                            )
                        except StopAsyncIteration:
                            break
                        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                            raise TransferError(
                                f"connection stalled at {done} on {filename}"
                            ) from exc
                        if chunk:
                            handle.write(chunk)
                            done += len(chunk)
                            if progress_callback:
                                progress_callback(filename, done, expected_size or done)
            except TransferError as exc:
                final_exc = exc
                if attempt < retries:
                    continue  # reconnect from *done* on the next attempt
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                final_exc = exc
                if attempt < retries:
                    continue
                break
            finally:
                await response.release()

            if expected_size > 0 and done < expected_size:
                final_exc = TransferError(
                    f"short body for {filename}: {done}/{expected_size} bytes"
                )
                if attempt < retries:
                    continue
                break
            if final_exc is None and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                if progress_callback:
                    progress_callback(filename, done, expected_size or done)
                log.info("single-stream download done", file=filename, size=_format_bytes(done))
                return filepath

        # loop exhausted / failure
        if progress_callback:
            progress_callback(filename, done, expected_size or done)
        raise final_exc or TransferError(f"single-stream download failed for {filename}")
    finally:
        if own_session:
            await session.close()
