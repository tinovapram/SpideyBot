# Delivery pipeline — decisions & implementation plan

**Status:** decision record + plan. Done so far: decisions 1–3, 6 (media budget) and 8 (sessionless
worker share). Not started: decisions 4, 5, 7, 9 and the streaming part of §4. Nothing committed
for the delivery work.
**Date:** 2026-10-22.

Legend: ✅ done · ◐ partly done · ⏳ planned (not started).

| Deliverable | Status | Evidence |
|:--|:--|:--|
| Album grouping (photos/videos only, ≤10) | ✅ | `utils/telethon.py`, `tests/test_send_album.py` |
| Delete local file right after upload | ✅ | `downloader/flow.py`, `downloader/terabox_flow.py` |
| Media/send pacing + leases (phase 1) | ✅ | `core/sendgate.py`, [SEND_RATE_LIMITING.md](./SEND_RATE_LIMITING.md) |
| Sessionless worker share (5 of 20, hard cap) | ✅ | `core/queue.py` `claim_next()`, `tests/test_sessionless_share.py`, §8 |
| Streaming `/dt` (resolve → download 1 → upload 1 → delete) | ⏳ | §4.1–4.2 |
| gallery-dl `-J`-only streaming, CLI fallback removed | ⏳ | §4.3 |
| Streaming depth parameter (4 site adapters / 1 gallery-dl) | ⏳ | §4.4 |
| Delivery tests (§4.5) | ⏳ | — |

---

## 1. Decisions taken in review

1. **✅ Done — Album rule.** Photos and videos are grouped into albums (max 10); documents,
   audio, archives and animations are sent individually (`utils/telethon.py`, commit
   `f4f3a20`, tests in `tests/test_send_album.py`).
2. **✅ Done — File lifecycle.** Keep deleting the local file right after upload.
   `prepare_media()` returns an `InputMediaUploaded*` holding the uploaded `InputFile`, so a
   retry re-sends the handle without a second upload. An expired Telegram **file reference**
   cannot be retried: the send fails fast (no retry) with `FileExpiredError`. The user-facing
   "media expired — please re-run" message is still a follow-up. (See `SEND_RATE_LIMITING.md`
   §5.5.)
3. **✅ Done (phase 1) — Media/send pacing.** [SEND_RATE_LIMITING.md](./SEND_RATE_LIMITING.md):
   one `SendGate` per client; the limited unit is **one prepared media** (one file, album or
   not), with media slots (user sessions = tier concurrency, bot client =
   `SPIDEY_MEDIA_CONCURRENCY`), a `SPIDEY_MEDIA_PER_MINUTE` rate, a per-chat message budget, a
   shared flood cooldown with jitter, retries and metrics. Phase 2 (adaptive rate) is not
   started.
4. **⏳ Planned — Pipeline.** Stream everything: download **one** file → upload it → delete it,
   instead of downloading a whole set first. Applies to `/dt` and to the gallery-dl path; the
   site adapters already do this.
5. **⏳ Planned — `/dt` specifics.** First resolve *which* messages carry media (including all
   members of an album, even when the link points into the middle of one), then download →
   upload → delete one by one. Goal: **disk saving** and earlier delivery.
6. **◐ Partly done — Sessionless majority.** Media is prepared on the user's own client (its
   tier concurrency is the media-slot budget) and the shared bot client keeps a conservative
   budget (`SPIDEY_MEDIA_CONCURRENCY`, `SPIDEY_MEDIA_PER_MINUTE`) — ✅ done. The worker share
   for sessionless users (§8) is ⏳ not enforced. Further ideas in §7.
7. **⏳ Planned — No gallery-dl CLI fallback.** `-J` becomes the only gallery-dl invocation. If
   the JSON carries no downloadable link, running the gallery-dl CLI would produce the same
   blank result, so the subprocess path is removed. The flow-level fallback to cyberdrop-dl
   stays.
8. **✅ Done — Sessionless worker share (hard cap).** Sessionless jobs may occupy at most
   `SPIDEY_SESSIONLESS_WORKER_SLOTS` (default **5**) of the 20 workers, regardless of tier; the
   rest is reserved for users with a live session. Queued sessionless users are told the split
   with a `/login` nudge. Details in §8.
9. **⏳ Planned — Streaming queue depth.** Site adapters keep their existing 4-deep
   download→upload queue; the gallery-dl path streams with depth **1** so exactly one file
   exists on disk per job; `/dt` is a plain sequential loop. Disk saving wins over
   download/upload overlap on the paths being rewritten.

## 2. Current behaviour (as audited)

| Path | Download ↔ upload | In-flight files | Album flush |
|:--|:--|:--|:--|
| `/dl` → site adapter | pipelined (producer/consumer) | queue depth **4** | every 10 |
| `/dl` → TeraBox | pipelined | queue depth **2** | every 10 + final |
| `/dl` → gallery-dl / cyberdrop | **all downloads first**, then uploads | entire set | at the end |
| `/dt` single / range | **all downloads first**, then uploads | entire set | at the end |

Two extra findings from the audit:

- `download_tg_range()` skipped every message after the first of a grouped album
  (`seen_groups`), so a range containing an album delivered only the **first** item. The new
  resolver expands albums instead (§4.1).
- Uploads are sequential **within** a job; parallelism exists only **across** jobs (up to
  `SPIDEY_MAX_CONCURRENT_DOWNLOADS` = 20), which is the flood exposure covered by the
  rate-limiting proposal.
- gallery-dl also has a **CLI download fallback** today (`_download_subprocess()`) used when
  `-J` fails or returns no URLs. It is removed by this plan — see §4.3.

## 3. Target behaviour — ⏳ not started

**`/dt` (single or range)**

```
resolve_tg_media(link)  → TGMediaPlan(messages=[…media messages…], caption, chat_title)
for msg in plan.messages:
    path  = download_tg_media(msg)      # one file on disk
    media = prepare_media(path)         # upload to Telegram
    delete(path)                        # disk freed immediately
    buffer += media
    if len(buffer) == 10: send_album(buffer)
send_album(remaining)
```

**`/dl` → gallery-dl**

`GalleryDLDownloader.download_streaming()` yields each finished path; the flow uploads,
deletes and flushes exactly like the `/dt` loop. `-J` is the **only** gallery-dl call: when the
JSON has no downloadable link the job does not retry through the gallery-dl CLI (it would be
blank too) — the flow falls through to cyberdrop-dl instead.

The cyberdrop-dl subprocess fallback still downloads everything before sending — documented
exception, it has no incremental API.

## 4. Implementation plan — ⏳ nothing started

### 4.1 `downloader/telegram.py` — resolve, then download one at a time ⏳

- `TGMediaPlan` dataclass: `ok`, `chat_title`, `caption`, `messages`, `total_messages`, `error`.
- `resolve_tg_media(client, url)` → single-message and range paths; keeps only messages with
  media; expands albums via `_fetch_group()` (±10 message window, same `grouped_id`).
- `download_tg_media(client, message, output_dir, progress_callback)` → `(path, metadata)`.
- Removes `download_tg_message()` / `download_tg_range()` (dict-based, download-everything).

### 4.2 `handler/handler.py` — `_download_tg` becomes the streaming loop ⏳

- Call `resolve_tg_media()` once; show "Downloaded x/N • Sent y/N" rows.
- No per-file cap: the whole plan is delivered (§8 limits concurrency, not content).
- Per message: `download_tg_media` → `prepare_media` → `os.remove(path)` in `finally` →
  append to the album buffer → `send_album` every 10, final flush at the end.
- Caption: `plan.caption` on the first item only (preserves today's intent).
- Per-file metadata mapping stays (`photo` / `animated` / `video` / `force_document`).

### 4.3 `downloader/gallerydl.py` — async streaming ⏳

- `download_streaming()` async generator: resolve URLs with `-J`, then yield the metadata
  sidecar (first, so the native caption lands on the first item) followed by each completed
  path.
- `_download_urls()` becomes `_iter_download_urls()` (the generator); `download()` becomes a
  thin collector so existing callers keep working.
- **Remove the CLI download fallback**: delete `_download_subprocess()` and its only helper
  `_dir_size()`, plus the `_PROGRESS_RE` / `_URL_RE` constants used solely by it. A `-J` run
  that yields no URLs raises, and `_run_gallerydl()` decides what happens next (cyberdrop-dl),
  instead of the downloader silently shelling out to the same blank result.

### 4.4 `downloader/flow.py` — consume the stream ⏳

- `_stream_upload(..., is_async=True, depth=...)` accepts an async source; the producer/consumer
  logic and album flushing stay shared with the site-adapter path. Depth is **4** for site
  adapters (unchanged) and **1** for gallery-dl, so the download of file *n+1* starts only after
  file *n* was uploaded and deleted (peak ≈ 1 file, §1.9).
- `_run_gallerydl()` and the streaming fallback use `_stream_gallerydl()` → `_stream_upload`.
- Fallback chain becomes: site adapter → gallery-dl (`-J` only) → cyberdrop-dl (subprocess).
- Fix the producer/consumer error path while there: on a producer failure, signal the consumer
  (`put(None)`) so pending uploads are still sent, then surface the error (today the consumer
  can be left waiting on a leaked task).
- `core/queue.claim_next()` applies the sessionless worker share (§8); a worker simply gets
  `None` while the share is full and waits on `wait_for_work()`.
- `DownloadManager._run_job()` **does** change — for the send gate, not for delivery: it leases
  the job's user client (`sessions.acquire`/`release`) and sets that client's **media slots**
  from the job tier (`SEND_RATE_LIMITING.md` §5.4).

### 4.5 Tests ⏳

1. `resolve_tg_media` — single message inside an album returns every member.
2. `resolve_tg_media` — range deduplicates albums and keeps order (regression for the old
   first-item-only bug).
3. `download_tg_media` — returns `(path, meta)`; returns `None` on download error.
4. `_stream_upload(is_async=True)` — with `depth=1` each file is uploaded and deleted before the
   next download starts; with `depth=4` at most 4 files are queued; albums still flush every 10.
5. `GalleryDLDownloader.download_streaming` — yields the metadata sidecar first, then paths as
   `_download_one` completes.
6. `-J` with no URLs raises; `_run_gallerydl()` then falls back to cyberdrop-dl instead of
   shelling out to gallery-dl again.
7. Sessionless worker share (see §8 tests): cap enforced at claim time, slot released on
   completion/cancel, admin and sessioned jobs unaffected, `0` disables.
8. Existing `tests/test_send_album.py` keeps covering the grouping rule.

## 5. Expected effect (once §4 lands)

| Metric | Before | After |
|:--|:--|:--|
| Peak disk for `/dt` range of 50 media | whole set | ~1 file (+ staging dir) |
| Time to first delivered file | after all downloads | after the first download |
| Peak disk for gallery-dl albums | whole set | ~1 file |
| Wall clock | download then upload | overlaps in the site-adapter path, sequential in gallery-dl/`/dt` |
| Reliability of `/dt` albums in ranges | only first member delivered | all members |
| gallery-dl failure path | retries the same empty result through a CLI subprocess | no subprocess; falls straight through to cyberdrop-dl |

## 6. Working-tree state at the time of writing

- **Committed:** media/send pacing (`22eed77`) and this note (`156377b`), plus the Docker stage
  restructure (`fd4fe2a`).
- **Uncommitted:** `/stop` (real command, keeps the session), the sessionless worker share
  (`core/queue.py`, `core/metrics.py`, `handler/handler.py` nudges), and the doc updates.
- **Delivery work (§4): ⏳ not started.** The partial `downloader/telegram.py` refactor was
  reverted; imports and the suite are green (87 tests).

## 7. Related notes from the same review

- **Sessionless majority.** A reserved worker share (§8, ✅ hard cap: 5 of 20); a media budget on
  the shared bot client (`SPIDEY_MEDIA_CONCURRENCY` slots, `SPIDEY_MEDIA_PER_MINUTE` tokens) so
  preparation cannot exceed a fixed budget (✅ done); same-type document albums (Bot API allows
  up to 10 documents of one type) to cut RPCs (⏳); a content-hash → uploaded `file_id` cache for
  repeat requests (⏳); session incentives (`/login` via QR, extra slot, "login for a faster
  lane") (⏳).
- **Global rate limiting.** See [SEND_RATE_LIMITING.md](./SEND_RATE_LIMITING.md).

## 8. Sessionless worker share (✅ implemented — hard cap)

**Rule:** at most `SPIDEY_SESSIONLESS_WORKER_SLOTS` (default **5**) of the
`SPIDEY_MAX_CONCURRENT_DOWNLOADS` workers (default 20) may run jobs for users **without a live
Telegram session**, regardless of tier. The remaining 15 slots stay reserved for users whose own
session is running. Sessionless users are told the split:

> 📋 Queued — position **#7**. Send `/cancel 12` to abort.
>
> ℹ️ Sessionless jobs share **5 of 20** worker slots. Send /login to get your own slot.

**Why:** every sessionless job competes for the same bot client's media/send budget; a fixed
share stops users who never logged in from occupying the whole pool, and turns the wait into a
concrete reason to log in. It is a **concurrency** control, not a content cap — the whole link
is still delivered.

**Decision: hard cap, not borrowing.** The cap binds always, so a logged-in user always finds a
free worker and per-chat queue positions stay predictable. Borrowing (using all 20 workers while
no sessioned job is queued) would shorten sessionless wall-clock when downloads dominate, but it
lets the sessionless population oversubscribe the shared bot account and makes "logged in =
faster" untrue in the common case. Revisit only if the idle-worker cost shows up on `/metrics`.

**Enforcement:** [`core/queue.py`](../core/queue.py) `claim_next()`

- A job is sessionless when `sessions.is_client_active(job.user_id)` is false at claim time
  (cheap dict lookup; no API call). The decision is recorded on the in-memory queue entry when
  the job is claimed, so terminal-state accounting never re-checks it (no database column).
  Queued jobs are re-evaluated on every claim, so a `/login` mid-queue takes effect immediately.
- While the number of RUNNING sessionless jobs is at the cap, sessionless jobs are skipped;
  sessioned and admin jobs are always eligible. A slot frees itself when a sessionless job
  reaches a terminal state (complete / fail / cancel), the same way per-user concurrency already
  works.
- Per-user concurrency applies on top (free 1, pro 2, premium 3), so a premium user without a
  session can occupy all 5 sessionless slots — see open point 2.
- Config `SPIDEY_SESSIONLESS_WORKER_SLOTS=0` disables the split.

**Details**

- **No content cap.** The whole link is delivered; only how many sessionless jobs run at once is
  limited.
- Queued sessionless jobs stay `QUEUED` — they are claimed as soon as a slot frees, nothing is
  dropped.
- Admins are exempt (`Job.is_admin` never counts as sessionless).
- Tiers keep their daily/bandwidth/concurrency limits; this share is orthogonal.
- The share is stated in the "Queued — position #N" reply for sessionless users and repeated in
  `/quota`, not only when the cap is actually hit.
- Metrics: `spideybot_sessionless_active` (gauge), `spideybot_sessionless_deferred_total`
  (claims skipped because of the cap) so `/login` uptake is measurable.

**Tests** — [`tests/test_sessionless_share.py`](../tests/test_sessionless_share.py), all ✅

1. ✅ Cap 5 with six sessionless jobs → five are claimed, the sixth stays `QUEUED`.
2. ✅ Completing a sessionless job frees its slot → the sixth is claimed.
3. ✅ A sessioned job is claimed while the share is full, ahead of the queued sessionless job.
4. ✅ `SPIDEY_SESSIONLESS_WORKER_SLOTS=0` → no restriction.
5. ✅ Admin jobs bypass the share, and the claimed job records `sessionless` correctly.
6. ✅ Deferred claims are counted in `sessionless_deferred`.

**Open points**

1. ~~Hard cap vs. borrowing~~ **Decided: hard cap** (see above). The comparison that drove it:

   | | Hard cap (chosen) | Borrowing (rejected) |
   |:--|:--|:--|
   | Sessionless workers | always ≤ 5 | ≤ 5 only while a sessioned job is queued |
   | Reservation for logged-in users | guaranteed | best-effort |
   | Capacity on a mostly-sessionless base | up to 15 workers idle | fully used |
   | Predictability for sessionless users | stable | varies with logged-in traffic |
2. **Open** — should a premium user without a session be able to take all five sessionless
   slots, or should the share be balanced per user?
3. ~~Should the share also cap concurrent sessionless *uploads* on the bot client?~~
   **Answered:** no separate cap — the media gate already bounds sessionless preparation
   (`SPIDEY_MEDIA_CONCURRENCY` slots + `SPIDEY_MEDIA_PER_MINUTE` tokens on the bot client).
