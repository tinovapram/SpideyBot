# V5 vs V4 — Difference Report

| | |
|---|---|
| Compared | `V5` (`bd6003e`) → `V4` (`b908e0e`, "Duplicate fix") |
| Date | 2026-10-08 |
| Reproduce | `git diff --stat b908e0e V5` |
| Scale | 10 files, 57 insertions / 335 deletions — V5 is **278 net lines smaller** |

`V5` is the replayed branch, based on `7385568` ("Allow admins to bypass concurrent
download limit") with the pre-rate-limit feature set re-applied. Everything below is what
`V4` has and `V5` does not.

---

## 1. Flat list (20 items)

### Missing from `utils/telethon.py` (8 functions)

1. `_random_id` — deterministic blake2b int64, the dedupe key Telegram matches on
2. `_dedupe_ids` — one stable id per item, disambiguating repeated captions
3. `_reply_to` — normalises Message/int to `types.InputReplyToMessage`
4. `_raw_call` — raw request factory with one flood-wait retry
5. `_rate_limit(client, kind="media")` — per-client throttle
6. `safe_upload_file` — flood-retry wrapper for uploads
7. `_send_album_batch` — the raw `SendMultiMediaRequest` call
8. `_send_items` — individual sends that reuse the album's ids

### Changed in `utils/telethon.py` (2 functions)

9. `safe_send_file` — here `(client, *args, **kwargs)`; V4 has
   `(client, entity, file, caption=None, **kwargs)` plus list→`send_album` routing
10. `send_album` — here Telethon `send_file` with per-item `safe_send_file` retry;
    V4 uses raw `SendMultiMediaRequest` / `SendMediaRequest` with per-item `random_id`
    reused in the fallback

### Missing changes in other files (9)

11. `core/sessions.py` — stale-client eviction in `start_client`
12. `handler/handler.py` — idempotent `register_user_handlers` (`_spidey_handlers_registered`)
13. `handler/handler.py` — `/dt` using `event.client` instead of `self.client`
14. `handler/handler.py` — `from utils.telethon import safe_send_file`
15. `core/worker.py` — `Q.fail(..., retry=not delivered)` gate
16. `core/queue.py` — `renew_lease()`
17. `downloader/terabox_flow.py` — `task.files_sent` accumulation in `_pipeline.flush()`
18. `downloader/flow.py` — `task.files_sent` in `_send_all_at_once`
19. `downloader/telegram.py` + `utils/files.py` — `safe_download_media` / `safe_upload_file`
    call sites restored

### Deliberately different here (1)

20. Tier concurrency defaults are `pro=2, premium=3` instead of `pro=3, premium=5`
    (commit `bd6003e`)

---

## 2. Per-file differences

| File | Δ | Removed content | Originating commit |
|---|---|---|---|
| `utils/telethon.py` | 234 → 81 lines | 8 functions, 2 rewritten | `b908e0e` + `9632fbd` |
| `core/worker.py` | −56 | `_renew_lease_loop`, `flow_task` restructure, retry gate | `3ce9a91` + `b908e0e` |
| `handler/handler.py` | ±35 | idempotency guard, `event.client` for `/dt`, `safe_send_file` import | `b908e0e` + `e6e6ef2` |
| `core/queue.py` | −19 | `renew_lease()`, `timedelta` import | `3ce9a91` |
| `core/sessions.py` | ±14 | stale-client eviction | `b908e0e` |
| `utils/files.py` | ±8 | `safe_upload_file` import + 3 call sites → raw `client.upload_file` | `9632fbd` |
| `downloader/terabox_flow.py` | ±5 | `task.files_sent` | `b908e0e` |
| `downloader/flow.py` | ±4 | `task.files_sent` in `_send_all_at_once` | `b908e0e` |
| `downloader/telegram.py` | ±4 | `safe_download_media` → raw `client.download_media` (2 sites) | `9632fbd` |
| `.env.example` | ±4 | pro 3→2, premium 5→3 | `bd6003e` (deliberate) |

Already present in `V5` (not part of the gap): the streaming→gallery-dl gate from
`f74e10c` — `if getattr(task, "files_sent", 0): raise` and `task.files_sent = sent` in
`_stream_upload.flush()`. Only the streaming path tracks partial delivery.

---

## 3. `utils/telethon.py` function surface

| Function | V4 (`b908e0e`) | V5 |
|---|---|---|
| `_random_id`, `_dedupe_ids`, `_reply_to`, `_raw_call` | yes | absent |
| `_rate_limit(client, kind="media")` | yes | absent |
| `safe_upload_file` | yes | absent |
| `_send_album_batch`, `_send_items` | yes | absent |
| `safe_edit` | yes, plus `_rate_limit(..., "control")` | yes, no rate limit |
| `safe_send_file` | `(client, entity, file, caption=None, **kwargs)` + list routing | `(client, *args, **kwargs)` |
| `safe_download_media` | yes | yes (identical) |
| `send_album` | raw API, ids reused in fallback | Telethon `send_file`, fresh identities |

---

## 4. Behavioural differences

1. **Album duplicates possible again.** The fallback re-sends items as new messages, so a
   partially delivered album is duplicated instead of completed.
2. **The same message can be handled twice.** A stale cached client is not evicted, and
   `register_user_handlers` has no idempotency guard.
3. **Partially delivered jobs re-run.** `core/worker.py` retries unconditionally, and the
   TeraBox / `_send_all_at_once` paths never set `task.files_sent`.
4. **`/dt` runs on the bot client.** `handler.py` passes `self.client` where `b908e0e` used
   `client = event.client` (the user's own session).
5. **No rate limiting, and no flood retry on transfers.** Because `safe_upload_file` is gone,
   `prepare_media` calls raw `client.upload_file()` with no `FloodWaitError` retry; likewise
   `downloader/telegram.py` calls raw `client.download_media()`. Only `safe_edit`,
   `safe_send_file` and `safe_download_media` still retry.
6. **Tier concurrency defaults** are 2/3 here versus 3/5 in V4.

---

## 5. Deferred commits and how to restore

All four are reachable on the pushed `V4` branch, so any can be reapplied with
`git cherry-pick <sha>`.

| Commit | Title | What it brings |
|---|---|---|
| `9632fbd` | add rate limiting for telegram api calls | `_rate_limit`, `safe_upload_file`, safe_* routing |
| `e6e6ef2` | Use event client for Telegram downloads and refine API rate limiting | `/dt` on `event.client`; rate-limit refinement; tier defaults (already applied separately) |
| `3ce9a91` | Renew job leases while downloads run to prevent duplicate re-claims | `queue.renew_lease`, `worker._renew_lease_loop` |
| `b908e0e` | Duplicate fix | `random_id` dedupe layer, stale-client eviction, idempotent registration, retry gate, `task.files_sent` |

Parked test coverage for the deferred duplicate work (untracked — `tests/` is gitignored):

- `tests/deferred_test_telethon_album.py`
- `_deferred_test_register_user_handlers_is_idempotent` in `tests/test_handler.py`

### Restore options

- **Duplicate safety only** — reapply `b908e0e` by hand or cherry-pick; it is independent of
  rate limiting. Item 5 above (lost flood retry) is caused by `9632fbd`, not `b908e0e`, so it
  stays missing unless `9632fbd` is also taken.
- **Full V4 behaviour** — switch back to the `V4` branch, or cherry-pick all four in order
  `9632fbd`, `e6e6ef2`, `3ce9a91`, `b908e0e`.

### Related branches

| Branch | Contents |
|---|---|
| `V5` | this branch — base `7385568` + pre-rate-limit features; renamed from `V4-core` on 2026-10-08 |
| `V4` | full original history (pushed) |
| `V3` | separate older line, shares base `f0ed9dc` with V4 |
| `V4-nolimit` | **deleted** — it was content-identical to `V5` apart from the tier defaults (3/5 vs 2/3). Its only unique commit was the revert `67ef936`, still reachable via `git reflog` / `git branch V4-nolimit 67ef936` |

---

## 6. Non-rate-limit feature inventory

Rate limiting is the only work intentionally excluded. Everything else below is either already
reapplied or still missing.

### Reapplied on `V5`

| Commit | Feature |
|---|---|
| `5a526f7` | vidmonstr downloader (`downloader/site/vidmonstr.py`, lazy entry in `downloader/registry.py`), telegram cleanup, telethon speedup dropped |
| `102819a` | Preserve the original caption when downloading from Telegram |
| `d4d58bf` | Per-tier concurrency limits enforced at claim time, plus queue mode |
| `b363dee` | `/cancel` and `/status` bot-side wrappers (`_handled_by_user_session`) so the bot stands down while a user session is live |
| `ecaf8f2` | Exclude expired job leases from active download counts |
| `8bdce1c` | Skip the gallery-dl fallback once streaming has already sent files |
| `feb27a3` | Flood-retry `safe_download_media` helper (hand-written on this branch) |
| `bd6003e` | Tier concurrency defaults `free=1, pro=2, premium=3` |

### Still missing — non-rate-limit

| Commit | Missing feature | Where it shows |
|---|---|---|
| `e6e6ef2` | Telegram downloads through the user's own client | `handler._download_tg` passes `self.client` to `download_tg_message` / `download_tg_range` (line 240/245) |
| `e6e6ef2` | Telegram sends routed through `safe_send_file` | `_download_tg` uses raw `self.client.send_file(...)` (line 295); `magic_user_handler` uses raw `event.client.send_file(...)` (line 649) |
| `3ce9a91` | Job lease renewal during long runs | `core/queue.py` has no `renew_lease`; `core/worker.py` has no `_renew_lease_loop` and awaits the flow directly instead of running it as a cancellable `flow_task` |
| `b908e0e` | `random_id` dedupe layer | 8 functions missing from `utils/telethon.py` (items 1–8 above) |
| `b908e0e` | Stale-client eviction | `core/sessions.py::start_client` |
| `b908e0e` | Idempotent handler registration | `handler/handler.py::register_user_handlers` |
| `b908e0e` | Worker retry gate | `core/worker.py` still uses `Q.fail(..., retry=True)` |
| `b908e0e` | `task.files_sent` tracking | `downloader/terabox_flow.py`, `_send_all_at_once` in `downloader/flow.py` |

### Excluded on purpose — rate-limit related

- `_rate_limit` / per-client throttle (`9632fbd`, and the two-lane split inside `b908e0e`)
- `safe_upload_file` and the `safe_download_media` routing in `utils/files.py` /
  `downloader/telegram.py` (`9632fbd`)

Note: the lost flood-wait retry on uploads and downloads belongs to this last group, so it is
not restored by taking the duplicate fix alone.
