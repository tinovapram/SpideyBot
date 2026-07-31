# SpideyBot Improvement Plan

> Last updated: 2026-07-30
> Status: PLANNING — Not yet implemented

---

## Table of Contents

1. [Task Cancellation (Inline Keyboard)](#1-task-cancellation-inline-keyboard)
2. [Queue System Redesign](#2-queue-system-redesign)
3. [Download & Upload Progress](#3-download--upload-progress)
4. [Error Handling & Resilience](#4-error-handling--resilience)
5. [Pipeline Download + Upload](#5-pipeline-download--upload)
6. [Scaling for 500+ Users](#6-scaling-for-500-users)
7. [Security Hardening](#7-security-hardening)
8. [Code Quality](#8-code-quality)
9. [Operational](#9-operational)
10. [Implementation Roadmap](#10-implementation-roadmap)

---

## 1. Task Cancellation (Inline Keyboard)

### Problem
Users have no way to stop a running or queued download. If a task is slow or
no longer needed, the user is stuck waiting.

### Solution
Attach an **Inline Keyboard "❌ Cancel" button** to every task status message.
Tapping the button cancels that specific task — whether it's queued, downloading,
or uploading.

### UX Flow

**When task is queued (waiting for a slot):**
```
⏳ **SpideyBot:** Task #3 queued (position 2 in queue)
Your active downloads: 1/1

[ ❌ Cancel ]
```

**When task is downloading:**
```
📥 **SpideyBot:** Downloading...
• Progress: [■■■■■□□□□□] 50.0%
• File: 2/5 (25.0 MB / 50.0 MB)
• Speed: 2.3 MB/s

[ ❌ Cancel ]
```

**When task is uploading:**
```
📤 **SpideyBot:** Uploading...
• Progress: [■■■□□□□□□□] 30.0%
• File: 1/3 (15.0 MB / 50.0 MB)

[ ❌ Cancel ]
```

**After cancellation:**
```
❌ **SpideyBot:** Download cancelled.
• Task #3 removed from queue.
• 2 partial file(s) cleaned up.
```
(Button removed after cancellation)

### Callback Data Format
```
cancel:{user_id}:{entry_id}
```
- `user_id` — ensures only the task owner can cancel
- `entry_id` — identifies the specific task

### Implementation Details

**New/modified files:**

| File | Changes |
|---|---|
| `spideybot/queue_manager.py` | Add `cancel_event` to `DownloadTask`, `active_tasks` dict, `cancel_task()` method |
| `spideybot/core/handlers/user.py` | Add `callback_query` handler for cancel buttons, `/cancel` command |
| `spideybot/downloaders/download_handler.py` | Check `cancel_event` at key points, update status to "❌ Cancelled" |
| `spideybot/downloaders/terabox_handler.py` | Check `cancel_event` at key points |
| `spideybot/utils/progress.py` | Add cancel button to progress messages |

**Cancel button helper:**
```python
from telethon import Button

def cancel_button(user_id: int, entry_id: int) -> list:
    """Create inline keyboard with a cancel button for a specific task."""
    return [[Button.inline("❌ Cancel", data=f"cancel:{user_id}:{entry_id}")]]
```

**Callback handler:**
```python
@bot.on(events.CallbackQuery(pattern=r"cancel:(\d+):(\d+)"))
async def cancel_callback(event):
    callback_user_id = int(event.pattern_match.group(1))
    entry_id = int(event.pattern_match.group(2))

    if event.sender_id != callback_user_id:
        await event.answer("This is not your task.", alert=True)
        return

    success = download_manager.cancel_task(entry_id)
    if success:
        await event.edit("❌ **SpideyBot:** Download cancelled.")
    else:
        await event.answer("Task already completed or not found.", alert=True)
```

**Check points for cancellation in download handlers:**
```
1. Before starting download
2. After each file download completes (before next file)
3. Before each upload starts
4. After each upload completes (before next file)
5. Before sending to user
```

**Edge cases:**
- Task finishes between user tapping Cancel and bot processing → "Task already completed" answer
- User taps Cancel on an already-cancelled task → same as above
- Task is cancelled during upload → cleanup uploaded handles, delete partial files
- Admin cancels another user's task → only if admin, with `admin_cancel:{user_id}:{entry_id}`

---

## 2. Queue System Redesign

### Current Problems
1. **Rejection on full slots** — Free user sees "global limit reached" with no recourse
2. **No queued task visibility** — User doesn't know their position in queue
3. **No queued task cancellation** — Can't cancel a task waiting in queue
4. **No priority aging** — Low-priority tasks can starve forever

### New Queue Behavior

**Rule: Never reject, always queue.**

| Scenario | Current (Bad) | New (Good) |
|---|---|---|
| User at concurrent limit | ❌ "user_limit" rejected | ✅ Queued with position shown |
| Global free limit reached | ❌ "global_limit" rejected | ✅ Queued, waits for slot |
| Queue depth exceeded (new) | N/A | ⚠️ "Queue full, try again later" |

**Priority formula (revised):**
```python
# Tier base: premium=1.0, free=2.0 (lower = higher priority)
# Priority aging: subtract 0.001 per second waited (prevents starvation)
# Queue position penalty: add 0.1 per task already queued by this user

age_bonus = min(0.5, time_in_queue * 0.001)  # Max 0.5 priority boost
queue_penalty = queued_count * 0.1
priority = tier_base + queue_penalty - age_bonus
```

**Queue status message (shown when queued):**
```
⏳ **SpideyBot:** Task queued.
• Position: #3 in queue
• Estimated wait: ~2 minutes
• Your active downloads: 1/1

[ ❌ Cancel ]
```

**Queue depth limits:**

| Tier | Max in Queue | Max Concurrent |
|---|---|---|
| Free | 3 | 1 |
| Premium | 10 | 3 |
| Admin | ∞ | 5 |

---

## 3. Download & Upload Progress

### Current Problem
Users see a static "⏳ Starting download..." message for the entire download
phase, then only upload progress appears. No feedback during download.

### Solution: Unified Task Progress

**New `TaskProgress` class** that tracks both phases:

```
┌─────────────────────────────────────────────────┐
│  TaskProgress                                    │
├─────────────────────────────────────────────────┤
│  phase: "downloading" | "uploading" | "sending" │
│  current_file: int                              │
│  total_files: int                               │
│  bytes_downloaded: int                          │
│  total_bytes: int                               │
│  speed: float (bytes/sec)                       │
│  started_at: float                              │
└─────────────────────────────────────────────────┘
```

**Status message examples:**

*Downloading multi-file (YouTube playlist):*
```
📥 **SpideyBot:** Downloading...
• Phase: Download | File 2/5
• Progress: [■■■■■□□□□□] 50.0% (25.0 MB / 50.0 MB)
• Speed: 2.3 MB/s • ETA: 11s

[ ❌ Cancel ]
```

*Uploading:*
```
📤 **SpideyBot:** Uploading...
• Phase: Upload | File 1/3
• Progress: [■■■□□□□□□□] 30.0% (15.0 MB / 50.0 MB)
• Speed: 1.1 MB/s • ETA: 32s

[ ❌ Cancel ]
```

*Single file (TeraBox):*
```
📥 **SpideyBot:** Downloading...
• File: movie.mp4 (450.0 MB)
• Progress: [■■■■■■□□□□] 60.0% (270.0 MB / 450.0 MB)
• Speed: 5.2 MB/s • ETA: 33s

[ ❌ Cancel ]
```

**Progress update rate:** Every 3-5 seconds (respecting Telegram flood limits)
**Always show final update** (100% or error)

### Per-Phase Progress Sources

| Source | Download Progress | Upload Progress |
|---|---|---|
| gallery-dl | Monitor download dir size changes | `ProgressCallback` (existing) |
| TeraBox | HTTP stream `Content-Length` + bytes read | `ProgressCallback` (existing) |
| Reddit (PRAW) | PRAW download chunk count | `ProgressCallback` (existing) |
| UniversalDownloader | HTTP stream monitoring | `ProgressCallback` (existing) |

---

## 4. Error Handling & Resilience

### Current Problems
1. **All-or-nothing failure** — If 1 of 5 files fails, ALL are lost (shutil.rmtree)
2. **Silent failures** — `except Exception: pass` in several places
3. **Task stuck after error** — Task status not properly updated

### Solution: Per-File Error Handling

**Rule: Send whatever succeeded, report what failed.**

```
✅ 4 of 5 files downloaded successfully.
❌ Failed: video_5.mp4 (network timeout)

📤 Sending your files...

[ ❌ Cancel ]
```

**Modified download flow:**
```python
downloaded_files = []
failed_files = []

for file_info in file_list:
    if task.cancel_event.is_set():
        break
    try:
        path = await download_file(file_info)
        downloaded_files.append(path)
    except Exception as e:
        failed_files.append((file_info.name, str(e)))
        logger.warning(f"Failed to download {file_info.name}: {e}")

# Always proceed with whatever we have
if downloaded_files:
    await upload_and_send(downloaded_files, task)
    if failed_files:
        await report_partial_failure(failed_files, task)
else:
    await report_total_failure(failed_files, task)
```

**Cleanup on completion:**
- Delete temp files after successful upload (existing behavior, keep)
- Delete temp files on cancellation (new)
- Delete temp files on total failure (new)

---

## 5. Pipeline Download + Upload

### Current Flow (Sequential)
```
Download: [====F1====][====F2====][====F3====][====F4====]
Upload:                                          [====F1====][====F2====][====F3====][====F4====]
Time: ────────────────────────────────────────────────────────────────────────────────────────────►
Total: Download_time + Upload_time
```

### New Flow (Pipelined)
```
Download: [====F1====][====F2====][====F3====][====F4====]
Upload:              [====F1====][====F2====][====F3====][====F4====]
Send:                         [send F1]  [send F2]  [send F3]  [send F4]
Time: ────────────────────────────────────────────────────────────────────►
Total: Download_time + max(Upload_time - Download_overlap)
```

### Implementation

**Approach: Async Producer-Consumer with Bounded Buffer**

```python
async def pipelined_upload(media_files, task, bot):
    """Download files and upload concurrently with bounded buffer."""
    upload_queue = asyncio.Queue(maxsize=2)  # Max 2 files buffered
    upload_sem = asyncio.Semaphore(2)        # Max 2 concurrent uploads

    async def producer():
        """Download files and put them in the upload queue."""
        for i, file_info in enumerate(media_files):
            if task.cancel_event.is_set():
                break
            path = await download_file(file_info, task)
            await upload_queue.put((i, path))
        await upload_queue.put(None)  # Sentinel

    async def consumer():
        """Upload files from the queue."""
        while True:
            item = await upload_queue.get()
            if item is None:
                break
            i, path = item
            async with upload_sem:
                handle = await upload_file(path, task)
                await send_to_user(handle, task)

    await asyncio.gather(producer(), consumer())
```

**When pipeline is beneficial:**
- Multi-file downloads (YouTube playlists, Reddit galleries)
- Large files where download and upload can overlap

**When to use simple sequential:**
- Single file downloads (most TeraBox links)
- Very small files (< 1MB)

**Decision logic:**
```python
if len(media_files) > 1 or total_size > 10_000_000:  # > 10MB or multi-file
    await pipelined_upload(media_files, task, bot)
else:
    await sequential_upload(media_files, task, bot)
```

---

## 6. Scaling for 500+ Users

### Current Limitations

| Metric | Current | At 500 Users |
|---|---|---|
| Free concurrent slots | 10 global | 98% of free users blocked |
| Free per-user | 1 | Fair but very limited |
| Premium per-user | 5 | Good but premium users rare |
| Queue visibility | None | Users don't know wait time |
| Starvation risk | High | Low-priority tasks never run |

### New Tier Configuration

```python
# config.py additions

# Per-user concurrency
MAX_CONCURRENT_FREE = 1
MAX_CONCURRENT_PREMIUM = 3
MAX_CONCURRENT_ADMIN = 5

# Queue depth limits
MAX_QUEUE_FREE = 3
MAX_QUEUE_PREMIUM = 10
MAX_QUEUE_ADMIN = float('inf')

# Rate limiting (sliding window)
RATE_LIMIT_WINDOW = 600  # 10 minutes
RATE_LIMIT_FREE = 3      # 3 downloads per 10 min
RATE_LIMIT_PREMIUM = 20  # 20 downloads per 10 min
RATE_LIMIT_ADMIN = float('inf')

# Global limits
GLOBAL_FREE_WORKERS = 10  # Shared pool for all free users
```

### Sliding Window Rate Limiter

```python
class SlidingWindowRateLimiter:
    """Per-user sliding window rate limiter."""

    def __init__(self):
        self.user_timestamps = {}  # user_id → [timestamp, ...]

    def is_allowed(self, user_id: int, limit: int, window: int) -> bool:
        now = time.time()
        cutoff = now - window
        timestamps = self.user_timestamps.get(user_id, [])
        # Remove expired timestamps
        timestamps = [t for t in timestamps if t > cutoff]
        self.user_timestamps[user_id] = timestamps
        if len(timestamps) >= limit:
            return False
        timestamps.append(now)
        return True

    def get_wait_time(self, user_id: int, limit: int, window: int) -> float:
        """Seconds until the next download is allowed."""
        now = time.time()
        timestamps = self.user_timestamps.get(user_id, [])
        if len(timestamps) < limit:
            return 0.0
        oldest = min(timestamps)
        return max(0.0, oldest + window - now)
```

### Fair Share Scheduling for Free Users

```python
async def calculate_priority(self, task: DownloadTask) -> float:
    """Calculate task priority with anti-starvation aging."""
    tier_base = 1.0 if task.is_premium or task.is_admin else 2.0

    # Anti-starvation: boost priority by wait time (max 0.5 boost)
    wait_time = time.time() - task.timestamp
    age_boost = min(0.5, wait_time * 0.001)

    # Queue position penalty: more queued tasks = lower priority
    user_queued = sum(1 for t in self._get_user_tasks(task.user_id) if t != task)
    queue_penalty = user_queued * 0.1

    return tier_base + queue_penalty - age_boost
```

### User-Facing Queue Status

When user sends a link while at limit:
```
⏳ **SpideyBot:** Queued (position #3)
• Your active downloads: 1/1
• Estimated wait: ~2 minutes
• Queue: 2 ahead of you

[ ❌ Cancel ]
```

---

## 7. Security Hardening

### Immediate (P0)

| Action | Details |
|---|---|
| Rotate all secrets | TG_API_ID, TG_API_HASH, bot token, SESSION_ENCRYPT_KEY, TeraBox cookie |
| Create `.env.example` | Placeholder values, no real secrets |
| Move hardcoded secrets | Reddit API secrets, SoundCloud token, YouTube auth → env vars |
| Verify `.gitignore` | Ensure `.env` is excluded |

### Important (P1)

| Action | Details |
|---|---|
| Rate limit admin commands | Cooldown on `/addpremium` (e.g., 1 per 30 seconds) |
| Sanitize logs | Never log tokens, cookies, or API keys |
| Parameterized queries | Verify all SQL uses parameterized queries |
| Input validation | URL length limits, malformed input handling |

---

## 8. Code Quality

### Type Hints
```python
# Before
def run_download_task(task, bot, fallback_downloader, reddit_downloader=None):

# After
async def run_download_task(
    task: DownloadTask,
    bot: TelegramClient,
    fallback_downloader: GalleryDLDownloader,
    reddit_downloader: RedditDownloader | None = None,
) -> None:
```

### Tests

| Module | Test Priority | Notes |
|---|---|---|
| `config.py` | High | Env loading, validation, helpers |
| `db.py` | High | CRUD operations, caching |
| `queue_manager.py` | High | Priority calculation, rate limiting, cancellation |
| `download_handler.py` | Medium | Pipeline logic, error handling |
| `utils/files.py` | Medium | Filename sanitization |
| Site downloaders | Low | External API mocking |

### DRY Fixes
- Extract `find_url()` from 9 site downloaders into `BaseDownloader`
- Extract common progress formatting into a shared utility

### Dead Code Cleanup
- Remove `hachoir`, `pillow`, `mutagen` from `requirements.txt`
- Remove or wire up `format_filetree()` in `formatting.py`
- Consider removing `spideybot/bot.py` redirect shim

---

## 9. Operational

### Graceful Shutdown
```python
import signal

def setup_signal_handlers(bot, queue_manager):
    loop = asyncio.get_event_loop()

    async def shutdown():
        logger.info("Shutdown signal received, stopping workers...")
        await queue_manager.stop_workers()
        await bot.disconnect()
        logger.info("Shutdown complete.")

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
```

### Health Check
```python
# Simple health endpoint on port 6414
from aiohttp import web

async def health_handler(request):
    return web.json_response({
        "status": "ok",
        "queue_size": queue_manager.queue.qsize(),
        "active_tasks": len(queue_manager.active_tasks),
        "workers": len(queue_manager.workers),
    })
```

### Structured Logging
```python
# Replace basicFormatter with structured JSON logging
import structlog

logger = structlog.get_logger()
logger.info("task_started", user_id=123, entry_id=5, link="https://...")
```

---

## 10. Implementation Roadmap

### Phase 1: Queue & Cancellation (Week 1)
```
Day 1-2: Core infrastructure
  ├── DownloadTask.cancel_event
  ├── TaskProgress class
  ├── Task status tracking (pending/downloading/uploading/completed/failed/cancelled)
  └── Sliding window rate limiter

Day 3-4: Queue manager upgrade
  ├── Never-reject queue behavior
  ├── Priority aging (anti-starvation)
  ├── Per-user queue depth limits
  ├── Cancel button on status messages
  └── /cancel command + callback_query handler

Day 5: Integration
  ├── Wire cancel_button into all status messages
  ├── Check cancel_event in download/upload loops
  └── Test cancellation at every phase
```

### Phase 2: Progress & Error Handling (Week 2)
```
Day 1-2: Progress system
  ├── TaskProgress integration with status messages
  ├── Download progress monitoring (dir size, HTTP stream)
  ├── Unified progress display (download + upload)
  └── Speed calculation + ETA

Day 3-4: Error handling
  ├── Per-file error handling (don't lose successful files)
  ├── Partial failure reporting
  ├── Proper cleanup on failure/cancellation
  └── Remove silent exception swallowing

Day 5: Testing
  ├── Unit tests for queue_manager
  ├── Unit tests for rate limiter
  └── Integration test for cancel flow
```

### Phase 3: Pipeline & Scaling (Week 3)
```
Day 1-2: Pipeline download+upload
  ├── Async producer-consumer pattern
  ├── Bounded buffer (maxsize=2)
  ├── Decision logic: pipeline vs sequential
  └── Integration with TaskProgress

Day 3-4: Scaling
  ├── Revised tier configuration
  ├── Dynamic global free slot allocation
  ├── Queue position display to users
  └── Estimated wait time calculation

Day 5: Testing & polish
  ├── Load test with simulated 500 users
  ├── Edge case testing
  └── Documentation update
```

### Phase 4: Security & Quality (Week 4)
```
Day 1-2: Security
  ├── Rotate secrets
  ├── Create .env.example
  ├── Move hardcoded secrets to env vars
  └── Rate limit admin commands

Day 3-4: Code quality
  ├── Type hints for core modules
  ├── Extract find_url() to BaseDownloader
  ├── Remove dead code and deps
  └── Add basic pytest suite

Day 5: Operational
  ├── Graceful shutdown (SIGTERM)
  ├── Health check endpoint
  └── Update Summary.md
```

---

## Summary: What Changes Where

### New Files
| File | Purpose |
|---|---|
| `spideybot/utils/task_progress.py` | Unified progress tracking class |
| `spideybot/utils/rate_limiter.py` | Sliding window rate limiter |
| `tests/test_queue_manager.py` | Queue manager unit tests |
| `tests/test_rate_limiter.py` | Rate limiter unit tests |
| `.env.example` | Placeholder env vars |

### Modified Files
| File | Changes |
|---|---|
| `spideybot/queue_manager.py` | Cancel support, rate limiting, never-reject queue, priority aging, queue status |
| `spideybot/core/handlers/user.py` | `/cancel` command, callback_query handler for cancel buttons |
| `spideybot/downloaders/download_handler.py` | Pipeline upload, per-file errors, cancel checks, progress integration |
| `spideybot/downloaders/terabox_handler.py` | Cancel checks, progress integration, per-file errors |
| `spideybot/utils/progress.py` | Integrate with TaskProgress, add cancel button |
| `spideybot/config.py` | New config values for rate limiting, queue limits, scaling |
| `requirements.txt` | Remove dead deps, add any new deps |
| `.env.example` | New env vars with placeholders |
| `Summary.md` | Update to reflect current architecture |
