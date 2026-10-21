# Media preparation & send rate limiting — design

**Status:** implemented (phase 1) in [`core/sendgate.py`](../core/sendgate.py), wired into
[`utils/telethon.py`](../utils/telethon.py), [`utils/files.py`](../utils/files.py),
[`core/sessions.py`](../core/sessions.py), [`core/worker.py`](../core/worker.py),
[`core/config.py`](../core/config.py), [`core/metrics.py`](../core/metrics.py) and
[`core/scheduler.py`](../core/scheduler.py). Uncommitted at the time of writing.
**Scope:** pacing of media preparation (`upload_file`) and message sends
(`send_file` / albums), plus flood-wait handling around them. Command rate limiting
(`/dl`, `/dt`) is out of scope — it already lives in [`core/ratelimit.py`](../core/ratelimit.py).

Legend: ✅ done · ◐ partly done · ⏳ planned (not started).

| Piece | Status | Where |
|:--|:--|:--|
| Gate registry, one per client | ✅ | `core/sendgate.py` |
| Token bucket with separate burst capacity | ✅ | `TokenBucket(rate, burst=…)` |
| Media tokens per client (`MEDIA_PER_MINUTE`) | ✅ | `SendGate.take_media()` |
| Media slots per client (`MEDIA_CONCURRENCY`, tier for user clients) | ✅ | `SendGate.media_slot()` |
| Thumbnail inside the same slot, token-free | ✅ | `utils/files.py` `prepare_media()` |
| Per-chat message budget (`SEND_CHAT_PER_MINUTE` + burst) | ✅ | `SendGate.send()` |
| Shared flood cooldown with jitter | ✅ | `_block()` / `_wait_unblocked()` |
| Retry taxonomy + `SEND_MAX_ATTEMPTS` | ✅ | §5.3 |
| Session leases so eviction cannot drop a client mid-upload | ✅ | `core/sessions.py`, `core/worker.py` |
| Metrics on `/metrics` | ✅ | `core/metrics.py` |
| Scheduler sweep of idle gates/buckets | ✅ | `core/scheduler.py` |
| Tests (§9) | ✅ | `tests/test_sendgate.py`, `tests/test_sessions.py` |
| Phase 2 adaptive rate (AIMD) | ⏳ | §8 |
| Open questions 1, 3, 4, 5 | ⏳ | §11 |

---

## 1. Why

The bot downloads with up to `SPIDEY_MAX_CONCURRENT_DOWNLOADS` workers (default **20**,
[`core/config.py`](../core/config.py), [`core/worker.py`](../core/worker.py)), and every worker
prepares and sends media with **no shared pacing**. Each job reacts to Telegram's `FLOOD_WAIT`
individually, after the fact. Under load this produces floods, retries in lockstep, and
occasionally loses media.

The limits are expressed in **prepared media**, not in messages: one file the user asked for
costs one token whether it ends up in an album of ten or on its own. That keeps the budget
predictable regardless of how the delivery layer groups files.

## 2. Implementation before the gate

| Aspect | Before | Reference |
|:--|:--|:--|
| Upload concurrency | one `upload_file()` per job → up to 20 parallel uploads on the same client | [`utils/files.py`](../utils/files.py) `prepare_media()` |
| Send pacing | album batches (≤10) sent back-to-back; documents sent individually, no delay | [`utils/telethon.py`](../utils/telethon.py) `send_album()` |
| Flood waits | `sleep(exc.seconds)` then a **single** retry, no jitter, no shared cooldown | [`utils/telethon.py`](../utils/telethon.py) `safe_send_file()`, `safe_edit()` |
| Individual retry | one retry after a flat `1.0 s` | [`utils/telethon.py`](../utils/telethon.py) `_send_individual()` |
| Status edits | per-message throttle (3 s content, 1.2 s pulse) | [`utils/progress.py`](../utils/progress.py) |
| Per-user limit | 12 commands/min, never applied to uploads/sends | [`core/ratelimit.py`](../core/ratelimit.py) |
| File lifecycle | local file deleted right after upload; the send reuses the uploaded handle | [`downloader/flow.py`](../downloader/flow.py), [`downloader/terabox_flow.py`](../downloader/terabox_flow.py) |
| Metrics | none for floods/retries | [`core/metrics.py`](../core/metrics.py) |

## 3. Failure modes this addresses

1. **Thundering herd.** Every coroutine that hit the same `FloodWait(seconds)` retries at the
   same instant when the wait expires. The retry burst is what triggers the second flood.
2. **Failed delivery.** A second `FloodWaitError` fails the send. The uploaded handle can be
   re-sent, but if the file reference expired meanwhile the batch can never complete and the
   user has to re-run the link.
3. **No fairness.** One user's 200-file request can occupy all workers and all media slots;
   other users see no progress.
4. **Reactive only.** The bot never paces itself — it waits to be throttled.
5. **Per-client blindness.** The bot client and each user-session client have independent
   Telegram limits, but nothing modelled that.
6. **Edit costs.** Every active job edits its own status message; many jobs in one chat add up
   to the same per-chat budget as sends.

## 4. Goals / non-goals

**Goals**

- Retries reuse the already-uploaded media handle; an expired file reference fails fast instead
  of looping.
- Keep delivery correct and reasonably fast under 20 concurrent jobs.
- Fairness between chats for *messages*, and one predictable budget for *media*.
- Media is prepared on the user's own session (tier concurrency); the shared bot client keeps a
  conservative budget.
- Observable: floods, waits, retries and media throughput visible on `/metrics`.
- Configurable without code changes; conservative defaults an operator can relax.

**Non-goals**

- Multi-process coordination (Redis). The bot is single-process by design; a second instance
  needs its own solution.
- Predicting Telegram's exact limits. They are undocumented and per-account; the design adapts
  instead.
- Limiting bytes or bandwidth. A 2 GB video and a 200 KB photo cost the same token; the slot
  pool (not the token) is what accounts for size.

## 5. Design

### 5.1 `core/sendgate.py` — one gate per Telethon client

```
SendGate
├── media_slots: bounded by MEDIA_CONCURRENCY (bot) / tier.concurrent (user)
├── media_bucket: TokenBucket(MEDIA_PER_MINUTE, burst=MEDIA_BURST)
├── chat_buckets: dict[chat_id, TokenBucket(SEND_CHAT_PER_MINUTE, burst=SEND_BURST)]
├── blocked_until: float                     # shared cooldown (monotonic)
└── counters: media prepared / waited, flood waits, retries
```

- **Per client**, not global: the bot client and each user client (`core.sessions`) have their
  own limits, so one user's session floods must not stall the bot.
- Gates live in a module-level registry keyed by `id(client)` (the gate keeps the client alive,
  so the key cannot be recycled) and are swept by the existing scheduler.
- The gate is the **only** place that talks to Telegram for uploads/sends:
  `utils/telethon.py` and `utils/files.py` route through it, so every flow inherits it
  (`/dl`, `/dt`, TeraBox, magic-word, future flows).

### 5.2 Admission algorithm

```
async def prepare(op):                  # utils/files.prepare_media()
    async with self.media_slot():       # 1) concurrency — heavy files
        await self.take_media()         # 2) rate — many small files
        return await op()               #    upload_file(file) + upload_file(thumb)

async def send(chat_id, op):            # one RPC per album or single file
    await self._wait_unblocked()        # shared cooldown + jitter
    await self._chat_bucket(chat_id).take()
    try:
        return await self._attempt(op)
    except (FloodWaitError, SlowModeWaitError) as exc:
        self._block(exc.seconds)        # cooldown for the whole client
        raise
```

**Why two media dimensions.** Without the token, five slots can churn hundreds of small files a
minute; without the slots, a burst of 60 tokens can start 60 large uploads at once. The token
answers *how many start*, the slot answers *how much is in flight*.

**Burst is separate from rate.** A bucket's capacity is `MEDIA_BURST` / `SEND_BURST`, not its
per-minute value, so a full bucket releases at most that many calls in the same instant and then
refills at `rate / 60` per second.

**Tokens are not refunded.** A retried send or upload is a real attempt against Telegram, so it
keeps its token (slightly conservative, never optimistic).

**One album = one chat token.** Telegram counts message RPCs, not media, so an album of ten
costs one token while ten documents cost ten. That is why the send budget stays **per chat** and
not per client: per media, `sends ≤ media prepared` already holds, so a per-client send bucket
would be redundant.

Worked example — a TeraBox job of 10 files:

| The 10 files are… | media tokens | media slots used | chat tokens |
|:--|--:|:--|--:|
| photos | 10 | 10 (one short upload each) | 1 (one album) |
| videos > 10 MB | 10 | 10 (thumbnail + file, one slot each) | 1 |
| archives / audio | 10 | 10 (one upload each) | 10 (one message each) |
| mixed | 10 | 10 | 1 + *n* |

### 5.3 Error taxonomy and retry policy

| Error | Action |
|:--|:--|
| `FloodWaitError`, `SlowModeWaitError` | cooldown via the gate, retry (counts toward attempts) |
| `TimeoutError`, `RPCError` code ≥ 500 | exponential backoff 2/4/8 s + jitter, retry |
| `FileReferenceExpiredError`, `FilePartMissingError` | **fail fast** with `FileExpiredError` — the local file is already gone, so a re-upload is impossible (§5.5) |
| `RPCError` 400-family (invalid media, too large) | fail fast, no retry |
| attempts exhausted | fail the batch, surface a clear error |

`SPIDEY_SEND_MAX_ATTEMPTS` (default 4) replaces the old "exactly one retry". Flood waits count
as attempts, so an endless flood cannot loop forever.

### 5.4 Media slots — per user session, tier-derived

`prepare_media()` wraps the whole transfer (file **and** thumbnail) in `gate.media_slot()` and
spends one token.

The job already picks the **user's own client** when their session is live
([`core/worker.py`](../core/worker.py): `sessions.get_client(user_id) or self.bot`), so media is
already prepared across accounts. The slot budget follows that split:

| Client | Media slots | Rationale |
|:--|:--|:--|
| user-session client | `tier.concurrent` (1 / 3 / 5) | Telegram limits are per account, and the tier already caps how many of *that user's* jobs run at once — no extra setting, no way to exceed it |
| bot client (no session, or session offline) | `SPIDEY_MEDIA_CONCURRENCY` (default 5; `0` follows `SPIDEY_SESSIONLESS_WORKER_SLOTS`) | this client is shared by every sessionless user, so it needs its own conservative budget that tracks the sessionless worker share |

Consequences:

- A Premium user can prepare five media in parallel on their own account while a Free user gets
  one.
- Flood isolation: a cooldown on a user-session gate stalls only that user; a cooldown on the bot
  gate stalls only sessionless users. This is the main argument for *per-client* gates over one
  process-wide gate.
- The shared bot client remains the only real bottleneck; the media token protects it, and users
  who log in via `/login` automatically move their traffic off it.

**Thumbnails are not a separate media.** `make_video_thumb()` runs in a thread *before* the slot
is taken (it is local CPU/disk, not Telegram traffic), then the file and its thumbnail are
uploaded inside the **same** slot, spending **one** token. A video therefore costs one unit of
budget and two uploads of wall-clock time. Counting the thumbnail would make the limit depend on
whether ffmpeg succeeded, which users cannot predict.

**Prerequisite — lease active clients.** [`core/sessions.py`](../core/sessions.py) evicts
clients idle for `SPIDEY_SESSION_IDLE_SECONDS` (600 s, swept every 60 s) and LRU-evicts when
`SPIDEY_SESSION_MAX_LIVE` is reached. `get_client()` only refreshes the idle timestamp when it is
called, so a long upload could be disconnected **mid-transfer**. `DownloadManager` now leases the
client for the job's duration (`sessions.acquire` / `release`), and both the idle sweep and the
LRU eviction skip leased clients.

### 5.5 File lifecycle — keep deleting after upload

Deleting the local file right after `prepare_media()` uploads it
([`downloader/flow.py`](../downloader/flow.py)) is fine and stays as-is: `prepare_media()`
returns an `InputMediaUploaded*` that holds the uploaded `InputFile`, so a retry re-sends that
handle **without re-uploading** and without needing the file on disk.

One failure path still has to be handled explicitly: Telegram's **file reference** expires
(typically hours, sooner under load). If a long flood cooldown outlives it, the retry raises
`FileReferenceExpiredError`; with the local file gone there is nothing to re-upload, so the send
fails fast as `FileExpiredError` (§5.3) instead of looping. Media already delivered in earlier
batches stays delivered.

If long floods turn out to be common in practice, the cheap middle ground is to keep files on
disk only while `gate.blocked_until` is in the future, so a re-upload is possible; that is a
follow-up, not part of phase 1.

### 5.6 Fairness

- **Messages** are paced per chat: all of one user's jobs share one bucket, so a 100-file request
  cannot consume the bot's whole message budget in one chat. Album batching (10 media per RPC)
  keeps the message count low.
- **Media** is paced per client, not per chat, because uploads are billed per account. A single
  user's large request is bounded by their own concurrency slots (tier) and, for sessionless
  users, by the worker share in [DELIVERY_PIPELINE.md](./DELIVERY_PIPELINE.md) §8.
- Edits spend no tokens and therefore cannot starve sends; they still respect the cooldown.

### 5.7 Integration points

| File | Change |
|:--|:--|
| `core/sendgate.py` | new module (gate registry, token buckets with burst, cooldown, retry helper, media slots and tokens) |
| `utils/telethon.py` | `safe_send_file`, `send_album` go through `gate.send()`; `safe_edit` through a lighter edit path (same cooldown, no token cost) |
| `utils/files.py` | `prepare_media` takes one media token and holds one media slot for the file **and** its thumbnail; ffmpeg runs outside the gate |
| `core/sessions.py` | `acquire()` / `release()` leases; eviction + LRU skip leased clients; clients labelled `bot` / `user:<id>` |
| `core/worker.py` | lease the job's client and set its media slots from the tier |
| `core/config.py` | new settings (below), `media_slot_budget()` |
| `core/metrics.py` | media/​send counters and gauges |
| `core/scheduler.py` | sweep idle gates and per-chat buckets |
| `core/bot.py` | label the bot client; reset gates on shutdown |
| `downloader/flow.py`, `downloader/terabox_flow.py` | **no change required by this design** — local files are still removed after upload (§5.5); `flow.py` is changed separately by `DELIVERY_PIPELINE.md` for streaming |

## 6. Configuration

| Setting | Default | Meaning |
|:--|--:|:--|
| `SPIDEY_MEDIA_CONCURRENCY` | 5 | media prepared in parallel on the shared **bot** client; `0` follows `SPIDEY_SESSIONLESS_WORKER_SLOTS`. User clients use their tier concurrency |
| `SPIDEY_MEDIA_PER_MINUTE` | 60 | media prepared per minute, **per client** |
| `SPIDEY_MEDIA_BURST` | 5 | media that may start in the same instant |
| `SPIDEY_SEND_CHAT_PER_MINUTE` | 20 | message RPCs per chat per minute |
| `SPIDEY_SEND_BURST` | 5 | message RPCs that may leave at once in one chat |
| `SPIDEY_FLOOD_JITTER_SECONDS` | 2.0 | random spread added to a cooldown or backoff, so waiters do not retry in lockstep |
| `SPIDEY_SEND_MAX_ATTEMPTS` | 4 | attempts per send before failing the batch |

Rate values are buckets, not delays. With the defaults, a chat may emit **5 messages at once**
then one every 3 s; a client may prepare **5 media at once** then one per second.

Because the unit is media, the numbers read the same for any media type:

- 10 photos → 10 media tokens (≈ 10 s at 60/min) and **1** chat token (one album).
- 10 archives → 10 media tokens and **10** chat tokens (≈ 30 s at 20/min).
- 10 large videos → 10 media tokens; each holds a slot for the length of its upload plus a
  thumbnail upload, so wall-clock is dominated by the slot pool, not the token rate.

`SPIDEY_MEDIA_CONCURRENCY` and `SPIDEY_SESSIONLESS_WORKER_SLOTS` describe the same population
(users without a session), so keep them equal; `0` makes that automatic.

## 7. Observability

New metrics (Prometheus text via `/metrics`):

```
spideybot_media_prepared_total
spideybot_media_wait_seconds_total
spideybot_media_in_flight
spideybot_send_flood_waits_total
spideybot_send_flood_waits_by_client_total{client="bot|user:<id>"}
spideybot_send_retries_total
spideybot_send_retries_by_reason_total{reason="flood|transient|expired"}
spideybot_send_gate_wait_seconds_total
spideybot_messages_sent_total
```

Chat ids are deliberately **not** used as a label (cardinality and privacy); per-chat fairness is
bucketed, not exported per chat.

Log at `warning` when a flood lasts more than 30 s, and at `info` when a cooldown expires.

## 8. Phase 2 — adaptive rate (optional) ⏳ not started

Telegram's limits depend on account age, DC and payload mix; hardcoded rates will be wrong for
some deployments. AIMD (additive increase / multiplicative decrease) applied to
`SPIDEY_MEDIA_PER_MINUTE`:

- on flood: `effective_rate = max(min_rate, effective_rate / 2)`, cooldown as reported;
- after 60 s without a flood: `effective_rate = min(configured_rate, effective_rate * 1.1)`;
- expose the current effective rate as a gauge.

Start with phase 1 fixed rates; enable AIMD only after observing a week of metrics.

## 9. Test plan — ✅ implemented

Unit tests with a fake client and a virtual clock (no network), in
[`tests/test_sendgate.py`](../tests/test_sendgate.py):

1. ✅ Burst is capped by capacity, not by the per-minute rate (`TokenBucket(60, burst=5)`).
2. ✅ Media tokens pace preparation (2/min, burst 2 → two start immediately, the third waits).
3. ✅ Media tokens are shared by every chat on the same client.
4. ✅ Chat buckets pace message RPCs and are independent per chat.
5. ✅ A flood of 5 s sets a shared cooldown; every waiter resumes after it, none before.
6. ✅ Flood retries stop at `SPIDEY_SEND_MAX_ATTEMPTS`; transient errors back off exponentially
   and re-send the **same** `InputMedia` (upload happens once).
7. ✅ An expired reference fails after exactly one attempt, and an expired album batch is
   dropped without a per-item retry storm.
8. ✅ `prepare_media` with a thumbnail spends **one** token and holds **one** slot for both
   uploads; three photos cost three tokens regardless of delivery grouping.
9. ✅ Media slots bound concurrency; raising the budget wakes a waiter.
10. ✅ Lease: `evict_idle_clients()` and the LRU cap skip a leased client
    ([`tests/test_sessions.py`](../tests/test_sessions.py)).

## 10. Risks and trade-offs

- **Throughput vs. safety.** Delivery gets slower for very large requests (100 archives at
  20 messages/min ≈ 5 minutes). That is the correct trade: today it floods and fails.
- **Latency.** A full bucket can still burst `*_BURST` calls; once empty, the wait per call is
  `60 / rate` seconds.
- **Complexity.** One new module plus thin wrappers at two choke points; business logic is
  untouched.
- **Session eviction.** Relying on user clients makes the idle sweep a correctness dependency;
  the lease in §5.4 is mandatory, not optional.
- **Tier concurrency is a ceiling, not a guarantee.** A Premium user with five slots still ends
  up queueing on their own account's budget; the tier value only bounds parallelism.
- **The bot client stays shared.** Users without a session all go through one client, so
  `SPIDEY_MEDIA_CONCURRENCY`, `SPIDEY_MEDIA_PER_MINUTE` and the per-chat buckets carry the load.
- **Thumbnails cost wall-clock, not budget.** A video occupies its slot for a thumbnail upload
  too, so large-video jobs are slower than the token rate alone suggests.
- **Wrong defaults.** Chosen conservatively; `/metrics` exists so they can be tuned.

## 11. Open questions

1. ⏳ **Open** — should the cooldown be **global across all clients** when the *bot* client
   floods? User sessions have separate limits, so probably not — but a shared "Telegram is
   unhappy" hint could avoid pile-ups.
2. ✅ **Answered** — ~~should uploads and sends share one budget, or two?~~ They are different
   units (media prepared vs message RPCs) and stay separate: media tokens per client, send
   tokens per chat.
3. ⏳ **Open** — do we want per-chat *state* limits (e.g. pause a chat after a `SlowModeWait` for
   its full slow-mode interval)? Likely yes — cheap to add once the gate exists.
4. ⏳ **Open** — should `/status` expose the current gate state (rate, blocked-for) to admins?
5. ⏳ **Open** — since a user session gives higher throughput (their own media budget), should
   the bot tell sessionless users that `/login` speeds up large downloads?
