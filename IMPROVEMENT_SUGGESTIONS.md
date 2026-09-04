# SpideyBot Improvement Suggestions

This review is based on the current Python package structure and the indexed call graph. It focuses on changes that improve maintainability, correctness, throughput, and ease of testing.

## Priority summary

| Priority | Area | Recommendation | Expected benefit |
|---|---|---|---|
| P0 | Queue correctness | Enforce per-user concurrency and implement real aging/fairness | Prevent resource monopolization and starvation |
| P0 | Download lifecycle | Split `run_download_task` into smaller services with one cleanup/error policy | Reduce failure paths and make behavior testable |
| P1 | Telegram ranges | Replace repeated group scans with a single-pass grouping algorithm | Change range processing from roughly O(n²) to O(n) grouping |
| P1 | Contracts | Replace untyped result dictionaries and broad exceptions with typed results and domain errors | Make callers safer and failures easier to diagnose |
| P1 | Test coverage | Add tests around queue scheduling, cancellation, cleanup, and downloader selection | Protect the most failure-prone behavior |
| P2 | Repository structure | Separate transport, application orchestration, domain models, and infrastructure | Make new downloaders and integrations cheaper to add |

## 1. Structure and architecture

### 1.1 Introduce explicit application services

`spideybot/downloaders/download_handler.py:276-427` contains platform detection, staging-directory creation, progress callbacks, status-message updates, download selection, upload selection, error reporting, and cleanup. The function has complexity 21 and cognitive complexity 62.

Split it into small collaborators with narrow responsibilities:

```text
spideybot/
  application/
    download_service.py       # use-case orchestration
    task_status.py             # Telegram status/progress updates
  domain/
    download_task.py
    download_result.py
    errors.py
  infrastructure/
    telegram_gateway.py
    filesystem_store.py
    downloader_registry.py
  downloaders/
    site_downloaders/
```

Suggested flow:

1. `DownloadService.resolve_source(link)` selects a downloader.
2. `DownloadService.download(source, request)` returns a typed stream/result.
3. `TelegramUploadService.send(files, metadata, progress)` handles uploading.
4. `CleanupService.remove_staging_directory(...)` runs from one `finally` block.

This also removes the unused `reddit_downloader` parameter from `run_download_task`, or gives it a defined role through dependency injection.

### 1.2 Make downloader registration data-driven

There are many site-specific downloader classes under `site_downloaders`. Keep each adapter focused on URL matching and site-specific extraction, but register them through a `DownloaderRegistry`:

```python
registry.register(YouTubeDownloader())
registry.register(RedditDownloader())
downloader = registry.resolve(url)
```

The registry should expose `resolve()` and `fallback`, so handlers do not need to know whether a source is handled by a site adapter, TeraBox, or gallery-dl.

### 1.3 Define boundaries around infrastructure

The dependency graph shows high fan-in for `utils`, `db`, and the downloader package. Keep handlers dependent on interfaces/protocols rather than concrete database, filesystem, HTTP, and Telegram implementations. This enables unit tests with fakes and prevents transport details from spreading into the application layer.

### 1.4 Centralize configuration

Move limits, staging paths, progress intervals, retry settings, and worker counts into one validated settings object. Pass that object into services instead of reading module-level configuration during execution. Validate values at startup and fail with an actionable configuration error.

## 2. Programming and correctness

### 2.1 Replace result dictionaries with typed contracts

`download_tg_message` and `download_tg_range` return dictionaries with optional and inconsistent keys (`error`, `captions`, `caption`, `file_metadata`, and counters). Introduce dataclasses or typed models:

```python
@dataclass
class DownloadResult:
    files: list[Path]
    captions: list[str]
    metadata: list[MediaMetadata]
    chat_title: str | None = None

@dataclass
class DownloadFailure:
    code: str
    message: str
```

Use exceptions for exceptional failures and reserve a result object for successful, possibly-empty output. This avoids repeated `ok` checks and makes static type checking useful.

### 2.2 Narrow exception handling

Several download paths catch `Exception`, log it, and continue or convert it into a user-facing string. Catch expected errors at the boundary where they can be handled (`ValueError`, HTTP errors, Telegram RPC errors, filesystem errors), and preserve the original exception with exception chaining. Unexpected exceptions should retain a correlation/task ID and a generic user message rather than exposing `str(e)` directly.

### 2.3 Make cleanup unconditional and safe

The main download runner removes the staging directory in the gallery-dl branch, while streaming follows a separate path. Put cleanup in `finally`, use `pathlib.Path`, and ensure cleanup cannot remove a path outside the configured download root. Add tests for success, cancellation, downloader failure, upload failure, and partial downloads.

### 2.4 Avoid mutating shared downloader state per task

`run_download_task` assigns a task-specific `_progress_callback` onto a downloader instance. If the instance is reused by concurrent workers, callbacks can overwrite one another. Pass progress callbacks as method arguments or create a per-operation context object. If a callback must be installed temporarily, restore it in `finally`.

### 2.5 Improve HTTP request contracts

`BaseDownloader._request` currently has many optional parameters and accepts arbitrary `**kwargs`. Introduce a request-options object or keyword-only parameters, use `Mapping`/`Any` annotations where appropriate, and define consistent retry, timeout, and response-size policies in the base class. Add structured error types for status code, timeout, and parsing failures.

### 2.6 Harden metadata parsing

`extract_post_text` has cognitive complexity 109 and recursively scans arbitrary JSON. Specific issues to address:

- `v['name']` can raise `KeyError` when an author object has no `name`.
- The same nested data can be traversed repeatedly after a match is already known.
- `category` and `author` are collected even though they are not caption candidates, mixing extraction and presentation.
- A fixed 1,024-character truncation policy is embedded in the parser.

Separate `extract_metadata()` from `format_caption()`, use safe `.get()` access, and make the key-priority policy configurable and testable.

### 2.7 Make shared state concurrency-safe

The database layer uses an in-memory user cache and persistence helpers. Define the cache ownership and synchronization policy explicitly. If multiple async tasks can update a user, protect read-modify-write operations with an async lock or use an atomic database upsert. Add tests for concurrent username/premium updates and cache invalidation.

## 3. Algorithms and performance

### 3.1 Fix Telegram album grouping from O(n²) to O(n)

`download_tg_range` currently scans `valid_messages` to determine whether a grouped message is the first, then scans it again to build the group. For `n` messages this can become quadratic.

Use one ordered grouping pass:

```python
groups: dict[int, list[Message]] = {}
ordered_items: list[Message | list[Message]] = []

for message in valid_messages:
    group_id = getattr(message, "grouped_id", None)
    if group_id is None:
        ordered_items.append(message)
        continue
    if group_id not in groups:
        groups[group_id] = []
        ordered_items.append(groups[group_id])
    groups[group_id].append(message)

for item in ordered_items:
    messages_to_download = item if isinstance(item, list) else [item]
    ...
```

This preserves message order while making grouping linear in the number of fetched messages.

### 3.2 Implement real queue fairness and per-user limits

`DownloadQueueManager` contains `user_active_counts` and `user_queues`, but the worker path consumes only `global_queue`; the per-user structures are not used to gate execution. Also, `add_task()` comments describe anti-starvation aging, but the priority remains a fixed tier value.

Choose one explicit scheduling design:

- **Weighted fair scheduling:** maintain one queue per user and select the next eligible user using deficit round-robin or weighted round-robin.
- **Aging priority queue:** store enqueue time and periodically compute `effective_priority = tier_priority - aging_rate * waited_seconds`, bounded to a configured range.

In both designs:

- increment/decrement `user_active_counts` in a `try/finally` block;
- enforce a maximum queued-task count per user;
- make cancellation remove or tombstone queued entries safely;
- use a monotonic clock for elapsed time;
- expose metrics for queue wait time, active tasks, and rejected/expired tasks.

### 3.3 Avoid linear queue-position lookups

`get_queue_position()` uses `_queued_ids.index(entry_id)`, which is O(n), and the list removal in the worker is also O(n). If exact position is required, maintain an indexed structure; if an estimate is acceptable, report priority, enqueue time, and number of higher-priority tasks instead of maintaining a second mutable list.

### 3.4 Bound memory and work for large ranges

`download_tg_range` creates a full `range` ID list, fetches all messages, and stores all file paths and metadata before returning. Add a maximum range size, process in chunks, and stream results to the upload layer where possible. This reduces peak memory and limits the impact of unusually large user requests.

### 3.5 Control progress-update pressure

Progress callbacks schedule Telegram edits from a worker thread/event loop. Use a per-task async rate limiter or debouncer with a single pending update; discard stale updates and close the updater when the task finishes. This avoids a backlog of scheduled edits during fast downloads.

## 4. Testing and quality gates

Add tests for the behaviors most likely to regress:

- queue: priority ordering, fair scheduling, per-user concurrency, cancellation before/after start, shutdown with queued work;
- download lifecycle: all downloader branches, cleanup on every exception, callback isolation between concurrent tasks;
- Telegram ranges: reversed/invalid ranges, deleted messages, albums at boundaries, duplicate grouped IDs, large ranges;
- metadata: malformed JSON, missing author name, nested lists, key precedence, truncation;
- HTTP: timeout, retryable status, non-retryable status, malformed response.

Add a CI quality gate with `pytest`, coverage for application/domain code, `ruff` (or equivalent), and `mypy`/`pyright` once typed contracts are introduced. Keep integration tests for Telegram and external sites separate from deterministic unit tests.

## 5. Suggested implementation order

1. Add typed domain models and focused tests without changing external behavior.
2. Fix queue accounting, cancellation cleanup, and scheduling fairness.
3. Extract `DownloadService`, upload/status handling, and cleanup behind interfaces.
4. Replace Telegram range grouping with a single-pass implementation and chunk large ranges.
5. Harden metadata parsing and HTTP contracts.
6. Add CI checks and lightweight runtime metrics.

