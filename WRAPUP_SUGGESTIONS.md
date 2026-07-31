# SpideyBot — Wrap-Up: Suggestions, Fixes & Improvements

> Last updated: 2026-07-30
> Status: Audit complete, ready for implementation

---

## Executive Summary

**41 Python files, ~3,400 lines of code, 15 site downloaders.**

### 5 Key Improvements

| # | Improvement | What It Does |
|---|-------------|--------------|
| **1** | **Task Cancellation** | Inline keyboard `❌ Cancel` button on every status message. Works for queued AND running tasks. |
| **2** | **Never-Reject Queue** | Tasks always queued with priority. User sees position # and estimated wait. |
| **3** | **Unified Progress** | Download + upload progress in one message. Shows bar, speed, ETA. |
| **4** | **Per-File Error Handling** | Don't lose successful files. Send what worked, report what failed. |
| **5** | **Pipeline Upload** | Download next file while uploading current. Multi-file tasks finish faster. |

### Bug & Quality Fixes

| Severity | Count | Category |
|----------|-------|----------|
| 🔴 Critical | 7 | Broken imports, blocking event loop, silent failures |
| 🟡 Medium | 8 | Hardcoded config, resource leaks, deprecated APIs |
| 🟢 Low | 12 | Missing type hints, naming, comment inconsistencies |
| **Total** | **27** | |

---

## 🔴 Critical Fixes (Do Now)

> **Note:** Reddit/YouTube/TikTok API tokens are public third-party tokens used by gallery-dl/yt-dlp.
> They are not sensitive — no rotation needed.

### 1. Broken Import — `bot.py:8`

```python
from spideybot.core.bot import main, bot, download_manager  # ❌ Doesn't exist
```

`core/bot.py` exports `download_queue_manager`, not `download_manager`. This will crash at import time.

**Fix:** Change to `download_queue_manager`.

### 4. `time.sleep(5)` Blocks Event Loop — `terabox_downloader.py:765`

```python
time.sleep(5)  # 🔴 Synchronous sleep in code called from async context
```

This blocks the entire event loop for 5 seconds (affects ALL 20 workers + bot).

**Fix:** Make the calling method async and use `await asyncio.sleep(5)`, or wrap the call in `run_in_executor`.

### 5. `terabox_handler.py:57` — `resolve()` Not Offloaded

`terabox_downloader.resolve()` does 9 sync HTTP requests directly on the event loop.

**Fix:** Wrap in `await loop.run_in_executor(None, ...)`.

---

## 🟡 Medium Priority Fixes

### 6. Silent Exception Swallowing (7 locations)

These `except Exception: pass` blocks hide real failures:

| File | Line | What's Hidden | Fix |
|------|------|---------------|-----|
| `queue_manager.py` | 179 | Malformed URLs silently proceed | Log + mark task failed |
| `terabox_downloader.py` | 590 | `_list_dir()` API failures return `[]` | Log at WARNING minimum |
| `terabox_downloader.py` | 620 | `_ensure_root_dir()` create failures | Log + retry |
| `terabox_downloader.py` | 749 | `_process_file()` fs_id resolution | Log + skip file with message |
| `terabox_downloader.py` | 847 | `resolve()` directory traversal | Log + raise |
| `download_handler.py` | 239 | Failed file sends silently dropped | Log which file failed |
| `user.py` | 38 | `parse_surl()` errors dropped | Log + show error to user |

**Fix pattern:**
```python
except Exception as e:
    logger.warning("Descriptive message: %s", e, exc_info=True)
```

### 7. Hardcoded Config Values (11 locations)

| File | Line | Value | Should Be |
|------|------|-------|-----------|
| `terabox_downloader.py` | 196 | `APP_ID = "250528"` | `TERABOX_APP_ID` env var |
| `terabox_downloader.py` | 197-200 | Chrome User-Agent string | Constant in config |
| `terabox_downloader.py` | 247 | `root_path: "/cloudvids"` | `TERABOX_ROOT_PATH` env var |
| `terabox_downloader.py` | 248 | `timeout: 30` | `TERABOX_TIMEOUT` env var |
| `base.py` | 6-9 | Chrome 120 User-Agent | `DEFAULT_USER_AGENT` constant |
| `db.py` | 168 | `86400` (seconds/day) | `SECONDS_PER_DAY = 86400` |
| `queue_manager.py` | 52 | `max_concurrent=20` | `config.MAX_CONCURRENT_DOWNLOADS` |
| `files.py` | 18 | `max_len=120` (filename) | Config constant |
| `progress.py` | 65 | Comment says 3s, code says 5s | Fix comment to match code |

### 8. Resource Leaks

| File | Issue | Fix |
|------|-------|-----|
| `terabox_downloader.py:275` | `requests.Session()` never closed | Add `close()` method + call in cleanup |
| `reddit_downloader.py:74,177,205,223` | Standalone `requests.get()` — no connection reuse | Use `requests.Session()` |
| `base.py:34,44` | New `requests.request()` per call — no connection reuse | Use `requests.Session()` in base class |

### 9. Deprecated API — `asyncio.get_event_loop()`

Used in 3 files. Deprecated since Python 3.10, removed in 3.12.

| File | Line | Fix |
|------|------|-----|
| `download_handler.py` | 66, 86, 96 | `asyncio.get_running_loop()` |
| `files.py` | 167 | `asyncio.get_running_loop()` |

---

## 🟢 Low Priority / Code Quality

### 10. Missing Type Hints (6 key functions)

| File | Function | Add Types |
|------|----------|-----------|
| `user.py:43` | `register_user_handlers(bot, download_manager)` | `bot: TelegramClient, download_manager: DownloadQueueManager` |
| `admin.py:18` | `register_admin_handlers(bot)` | `bot: TelegramClient` |
| `download_handler.py:24` | `run_download_task(task, bot, ...)` | All params typed |
| `terabox_handler.py:19` | `run_terabox(task, bot, terabox_downloader)` | All params typed |
| `queue_manager.py:52` | `__init__(self, bot, ...)` | All params typed |
| `config.py:24` | `validate_telegram_config()` | Return `-> bool` |

### 11. Naming Inconsistencies

| File | Line | Issue | Fix |
|------|------|-------|-----|
| `terabox_handler.py:77` | `f"./downloads/tb_{task.user_id}_{task.entry_id}"` | Uses `tb_` prefix | Change to `dl_` to match `download_handler.py` |
| `user.py:43` | Parameter named `download_manager` | Actually `DownloadQueueManager` | Rename to `queue_manager` |

### 12. Comment/Code Mismatch — `progress.py:65`

Comment says "Rate limit updates to every **3 seconds**" but the actual threshold at line 62 is **5 seconds**. Fix the comment.

---

## 📐 Architecture Improvements

### 13. Split `terabox_downloader.py` God Class (~1,300 lines)

**Current:** 4 data classes + 4 exception classes + `TeraBoxDownloader` class + CLI entry point — all in one file.

**Proposed split:**

```
spideybot/downloaders/terabox/
    __init__.py          # Re-exports for backward compat
    models.py            # TeraBoxFile, TeraBoxFolder, TeraBoxFileList, TeraBoxResolveResult
    exceptions.py        # TeraBoxError, TeraBoxAuthError, TeraBoxRateLimitError, TeraBoxUnavailableError
    downloader.py        # TeraBoxDownloader class (API client)
    cli.py               # __main__ block (CLI entry point)
```

### 14. Add Connection Pooling to `BaseDownloader`

**Current:** Every `_request()` and `_download_file()` call creates a new TCP connection.

**Fix:** Add `requests.Session()` to `__init__` with connection pooling:

```python
class BaseDownloader:
    def __init__(self):
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
```

---

## 🛡️ Security Improvements

### 15. `.env.example` Needs All Variables

Currently `.env.example` exists but may be incomplete. Audit all `config.py` env vars and ensure every one is documented in `.env.example` with placeholder values and descriptions.

---

## 📋 Implementation Phases

### Phase 1 — Quick Fixes (1-2 hours)

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| 1 | Fix broken import in `bot.py` | `bot.py` | 1 min |
| 2 | Fix deprecated `get_event_loop()` | `download_handler.py`, `files.py` | 5 min |
| 3 | Fix comment/code mismatch in `progress.py` | `progress.py` | 1 min |
| 4 | Add `logger.warning` to silent exception handlers | 7 files | 30 min |
| 5 | Fix `time.sleep(5)` blocking event loop | `terabox_downloader.py` | 15 min |
| 6 | Wrap `resolve()` in `run_in_executor` | `terabox_handler.py` | 5 min |
| 7 | Fix `tb_` directory prefix inconsistency | `terabox_handler.py` | 1 min |

### Phase 2 — Security & Config (1-2 hours)

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| 1 | Move hardcoded config to env vars | `terabox_downloader.py`, `config.py` | 30 min |
| 2 | Complete `.env.example` with all variables | `.env.example` | 15 min |
| 3 | Rotate Telegram bot token (exposed in chat history) | BotFather | 5 min |

### Phase 3 — Architecture (4-6 hours)

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| 1 | Split `terabox_downloader.py` god class | New `terabox/` package (5 files) | 2 hours |
| 2 | Add connection pooling to `BaseDownloader` | `base.py` | 30 min |
| 3 | Add type hints to key functions | 6 files | 1 hour |
| 4 | Add `Session.close()` to TeraBox downloader | `terabox_downloader.py` | 15 min |
| 5 | Switch to `httpx` async HTTP | `base.py`, all 15 downloaders | 3 hours |

### Phase 4 — Features (from IMPROVEMENT_PLAN.md)

| # | Feature | Effort |
|---|---------|--------|
| 1 | Task cancellation with inline keyboard | 1-2 days |
| 2 | Queue redesign (never-reject, priority aging) | 1 day |
| 3 | Unified progress display | 1 day |
| 4 | Resilient error handling | 1 day |
| 5 | Pipeline download+upload | 2-3 days |

---

## 📊 Before/After Snapshot

| Metric | Current | After Fixes |
|--------|---------|-------------|
| Silent exception handlers | 14 | 0 (all log) |
| Blocking event loop calls | 2 | 0 |
| Deprecated API calls | 3 | 0 |
| Files >500 lines | 1 (1300 lines) | 0 |
| Type hints on key functions | 0/6 | 6/6 |
| Connection pooling | 0/4 | 4/4 |
