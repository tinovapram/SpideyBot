# Upload & send rate limiting — design proposal

**Status:** proposal, not implemented.
**Scope:** pacing of Telegram uploads (`upload_file`), message sends (`send_file` / albums) and
the flood-wait handling around them. Command rate limiting (`/dl`, `/dt`) is out of scope — it
already exists in [`core/ratelimit.py`](../core/ratelimit.py).

---

## 1. Why

The bot downloads with up to `SPIDEY_MAX_CONCURRENT_DOWNLOADS` workers (default **20**,
[`core/config.py`](../core/config.py), [`core/worker.py`](../core/worker.py)), and every worker
uploads and sends with **no shared pacing**. Each job reacts to Telegram's `FLOOD_WAIT`
individually, after the fact. Under load this produces floods, retries in lockstep, and
occasionally loses media.

## 2. Current implementation

| Aspect | Today | Reference |
|:--|:--|:--|
| Upload concurrency | one `upload_file()` per job → up to 20 parallel uploads on the same client | [`utils/files.py`](../utils/files.py) `prepare_media()` |
| Send pacing | album batches (≤10) sent back-to-back; documents sent individually, no delay | [`utils/telethon.py`](../utils/telethon.py) `send_album()` |
| Flood waits | `sleep(exc.seconds)` then a **single** retry, no jitter, no shared cooldown | [`utils/telethon.py`](../utils/telethon.py) `safe_send_file()`, `safe_edit()` |
| Individual retry | one retry after a flat `1.0 s` | [`utils/telethon.py`](../utils/telethon.py) `_send_individual()` |
| Status edits | per-message throttle (3 s content, 1.2 s pulse) | [`utils/progress.py`](../utils/progress.py) |
| Per-user limit | 12 commands/min, never applied to uploads/sends | [`core/ratelimit.py`](../core/ratelimit.py) |
| File lifecycle | local file deleted right after upload; the send reuses the uploaded handle | [`downloader/flow.py`](../downloader/flow.py), [`downloader/terabox_flow.py`](../downloader/terabox_flow.py) |
| Metrics | none for floods/retries | [`core/metrics.py`](../core/metrics.py) |

## 3. Failure modes

1. **Thundering herd.** Every coroutine that hit the same `FloodWait(seconds)` retries at the
   same instant when the wait expires. The retry burst is what triggers the second flood.
2. **Failed delivery.** A second `FloodWaitError` fails the send. The uploaded handle can be
   re-sent, but if the file reference expired meanwhile the batch can never complete and the
   user has to re-run the link.
3. **No fairness.** One user's 200-file request can occupy all workers and all upload slots;
   other users see no progress.
4. **Reactive only.** The bot never paces itself — it waits to be throttled.
5. **Per-client blindness.** The bot client and each user-session client have independent
   Telegram limits, but nothing models that.
6. **Edit costs.** Every active job edits its own status message; many jobs in one chat add up
   to the same per-chat budget as sends.

## 4. Goals / non-goals

**Goals**

- Retries reuse the already-uploaded media handle; an expired file reference fails fast with a
  clear message instead of looping.
- Keep delivery correct and reasonably fast under 20 concurrent jobs.
- Fairness between chats: one large request must not starve others.
- Spread uploads over the users' own sessions (tier concurrency) and keep the shared bot client
  conservative.
- Observable: floods and waits visible on `/metrics`.
- Configurable without code changes; conservative defaults that an operator can relax.

**Non-goals**

- Multi-process coordination (Redis). The bot is single-process by design; a second instance
  needs its own solution.
- Predicting Telegram's exact limits. They are undocumented and per-account; the design
  adapts instead.

## 5. Design

### 5.1 `core/sendgate.py` — one gate per Telethon client

```
SendGate
├── upload_semaphore: asyncio.Semaphore(SPIDEY_UPLOAD_CONCURRENCY)
├── chat_buckets: dict[chat_id, TokenBucket]      # SPIDEY_SEND_PER_MINUTE
├── client_bucket: TokenBucket                     # SPIDEY_SEND_GLOBAL_PER_MINUTE
├── blocked_until: float                           # shared cooldown (monotonic)
└── flood_count / gate_wait / retry counters
```

- **Per client**, not global: the bot client and each user client (`core.sessions`) have their
  own limits, so one user's session floods must not stall the bot.
- Gates live in a module-level registry keyed by client (`id(client)` or user id) and are
  swept by the existing scheduler when idle.
- The gate is the **only** place that talks to Telegram for uploads/sends:
  `utils/telethon.py` and `utils/files.py` route through it, so every flow inherits it
  (`/dl`, `/dt`, TeraBox, magic-word, future flows).

### 5.2 Admission algorithm

```
async def send(self, chat_id, op):
    await self._wait_until_unblocked()        # shared cooldown + jitter
    await self._take_token(self.chat_buckets[chat_id])
    await self._take_token(self.client_bucket)
    try:
        return await self._attempt(op)        # op = client.send_file(...)
    except (FloodWaitError, SlowModeWaitError) as exc:
        self._block(exc.seconds)              # sets blocked_until for the whole client
        raise
```

- `_block(seconds)`: `blocked_until = now + seconds + uniform(0, jitter)`; a single
  `asyncio.Event` wakes every waiter once the cooldown expires, instead of each coroutine
  sleeping and re-firing independently.
- Tokens refill continuously (`capacity / 60` per second); a bucket never blocks for more than
  `60 / rate` seconds, which keeps latency predictable.

### 5.3 Error taxonomy and retry policy

| Error | Action |
|:--|:--|
| `FloodWaitError`, `SlowModeWaitError` | cooldown via the gate, retry (counts toward attempts) |
| `TimeoutError`, `RPCError` code ≥ 500 | exponential backoff 2/4/8 s + jitter, retry |
| `FileReferenceExpiredError`, `FilePartMissingError` | re-upload the file, retry |
| `RPCError` 400-family (invalid media, too large) | fail fast, no retry, notify the user |
| attempts exhausted | fail the batch, **keep the local files**, surface a clear error |

`SPIDEY_SEND_MAX_ATTEMPTS` (default 4) replaces the current "exactly one retry".

### 5.4 Upload slots — per user session, tier-derived

`prepare_media()` wraps `client.upload_file()` in `async with gate.upload_slot():`.

The job already picks the **user's own client** when their session is live
([`core/worker.py`](../core/worker.py): `sessions.get_client(user_id) or self.bot`), so
uploads are already spread across accounts. The slot budget follows that split:

| Client | Upload slots | Rationale |
|:--|:--|:--|
| user-session client | `tier.concurrent` (1 / 3 / 5) | Telegram limits are per account, and the tier already caps how many of *that user's* jobs run at once — no extra setting, no way to exceed it |
| bot client (no session, or session offline) | `SPIDEY_UPLOAD_CONCURRENCY` (default 2) | this client is shared by every sessionless user, so it needs its own conservative cap |

Consequences:

- A user's uploads never exceed their tier's concurrency, so a Premium user can run five
  parallel uploads on their own account while a Free user gets one.
- Flood isolation: a cooldown on a user-session gate stalls only that user; a cooldown on the
  bot gate stalls only sessionless users. This is the main argument for *per-client* gates over
  one process-wide gate.
- The shared bot client remains the only real bottleneck; the per-chat send buckets protect it,
  and users who log in via `/login` automatically move their traffic off it.

**Prerequisite — lease active clients.** [`core/sessions.py`](../core/sessions.py) evicts
clients idle for `SPIDEY_SESSION_IDLE_SECONDS` (600 s, swept every 60 s) and LRU-evicts when
`SPIDEY_SESSION_MAX_LIVE` is reached. `get_client()` only refreshes the idle timestamp when it
is called, so an upload longer than the idle window can be disconnected **mid-transfer**. Before
relying on user clients for uploads, acquire a lease for the job's duration
(`sessions.acquire(user_id)` / `release()`), make `evict_idle_clients()` and the LRU eviction
skip leased clients, and add a test for it.

### 5.5 File lifecycle — keep deleting after upload

Deleting the local file right after `prepare_media()` uploads it
([`downloader/flow.py`](../downloader/flow.py)) is fine and stays as-is: `prepare_media()`
returns an `InputMediaUploaded*` that holds the uploaded `InputFile`, so a retry re-sends that
handle **without re-uploading** and without needing the file on disk.

One failure path still has to be handled explicitly: Telegram's **file reference** expires
(typically hours, sooner under load). If a long flood cooldown outlives it, the retry raises
`FileReferenceExpiredError`; with the local file gone there is nothing to re-upload, so:

- detect that error and fail the batch fast with "media expired — please re-run /dl"
  (already part of the error taxonomy in §5.3);
- do **not** blindly retry — it can never succeed without a new upload;
- media that was already delivered in earlier batches stays delivered.

If long floods turn out to be common in practice, the cheap middle ground is to keep files on
disk only while `gate.blocked_until` is in the future, so a re-upload is possible; that is a
follow-up, not part of phase 1.

### 5.6 Fairness

The per-chat bucket gives fairness for free: all of one user's jobs share one bucket, so a
100-file request cannot consume the bot's whole send budget. Within a chat, album batching
(10 items per RPC) keeps the message count low.

### 5.7 Integration points

| File | Change |
|:--|:--|
| `core/sendgate.py` | new module (gate registry, token bucket, cooldown, retry helper, upload slots) |
| `utils/telethon.py` | `safe_send_file`, `send_album` go through `gate.send()`; `safe_edit` through a lighter edit path (same cooldown, no token cost) |
| `utils/files.py` | `prepare_media` uses `gate.upload_slot()` (per-client budget) |
| `core/sessions.py` | `acquire()` / `release()` leases; eviction + LRU skip leased clients |
| `core/worker.py` | lease the job's client for the duration of the job |
| `core/config.py` | new settings (below) |
| `core/metrics.py`, `core/observability.py` | new counters/gauges |
| `core/scheduler.py` | sweep idle per-chat buckets |
| `downloader/flow.py`, `downloader/terabox_flow.py` | **no change** — local files are still removed after upload (§5.5) |

## 6. Configuration

| Setting | Default | Meaning |
|:--|--:|:--|
| `SPIDEY_UPLOAD_CONCURRENCY` | 2 | parallel `upload_file()` calls on the shared **bot** client (user clients use their tier concurrency instead) |
| `SPIDEY_SEND_PER_MINUTE` | 20 | sends per chat per minute |
| `SPIDEY_SEND_GLOBAL_PER_MINUTE` | 60 | sends per client per minute |
| `SPIDEY_FLOOD_JITTER_SECONDS` | 2.0 | random extra cooldown to de-synchronise retries |
| `SPIDEY_SEND_MAX_ATTEMPTS` | 4 | attempts per send before failing the batch |

Rate values are buckets, not delays: a burst of 10 album sends still leaves immediately if the
bucket is full, then refills at `rate / 60` per second.

## 7. Observability

New metrics (Prometheus text via `/metrics`):

```
spideybot_send_flood_waits_total{client="bot"}
spideybot_send_retries_total{reason="flood|transient|expired"}
spideybot_send_gate_wait_seconds_total
spideybot_uploads_in_flight
spideybot_messages_sent_total{chat="…"}     # consider hashing/omitting chat ids
```

Log at `warning` when a flood lasts more than 30 s, and at `info` when a cooldown expires.

## 8. Phase 2 — adaptive rate (optional)

Telegram's limits depend on account age, DC and payload mix; hardcoded rates will be wrong for
some deployments. AIMD (additive increase / multiplicative decrease):

- on flood: `effective_rate = max(min_rate, effective_rate / 2)`, cooldown as reported;
- after 60 s without a flood: `effective_rate = min(configured_rate, effective_rate * 1.1)`;
- expose the current effective rate as a gauge.

Start with phase 1 fixed rates; enable AIMD only after observing a week of metrics.

## 9. Test plan

Unit tests with a fake client (no network):

1. 20 concurrent sends, bucket = 2/min → at most 2 calls in the first second, all eventually sent.
2. A flood of 5 s on one send → no other send happens before `blocked_until`; all resume after.
3. Flood twice → retried up to `SPIDEY_SEND_MAX_ATTEMPTS`, then the batch fails; retries
   re-send the **same** `InputMedia` (assert `upload_file` was called once).
4. Documents mixed with photos → albums only for photos/videos (already covered by
   `tests/test_send_album.py`), all paced by the same bucket.
5. Upload slots: a user client never exceeds its tier concurrency; the bot client never exceeds
   `SPIDEY_UPLOAD_CONCURRENCY`.
6. Per-chat fairness: two chats, one with 100 files → both make progress.
7. Expired reference: a `FileReferenceExpiredError` fails fast after one attempt (no re-upload
   loop) and does not fail batches that were already sent.
8. Lease: `evict_idle_clients()` skips a client leased by a running job, so a 12-minute upload
   is not disconnected mid-transfer.

A "chaos" fake that raises `FloodWaitError` randomly is worth keeping as an integration test
that asserts no lost files and bounded RPC rate.

## 10. Risks and trade-offs

- **Throughput vs. safety.** Delivery gets slower for very large requests (a 100-photo dump
  takes minutes). That is the correct trade: today it floods and fails.
- **Latency.** A full token bucket can still burst; once empty, the wait per send is
  `60 / rate` seconds.
- **Complexity.** One new module plus a thin wrapper at two choke points; business logic is
  untouched.
- **Session eviction.** Relying on user clients for uploads makes the idle sweep a correctness
  dependency; the lease in §5.4 is mandatory, not optional.
- **Tier concurrency is a ceiling, not a guarantee.** A Premium user with five slots still ends
  up queueing on their own account's send budget; the tier value only bounds parallelism.
- **The bot client stays shared.** Users without a session (the common case early on) all go
  through one client, so `SPIDEY_UPLOAD_CONCURRENCY` and the per-chat buckets carry the load.
- **Wrong defaults.** Chosen conservatively; `/metrics` exists so they can be tuned.

## 11. Open questions

1. Should the cooldown be **global across all clients** when the *bot* client floods? User
   sessions have separate limits, so probably not — but a shared "Telegram is unhappy" hint
   could avoid pile-ups.
2. Should uploads and sends share one budget, or two? Proposal: two (upload slots + send
   tokens), because their cost profiles differ.
3. Do we want per-chat *state* limits (e.g. pause a chat after a `SlowModeWait` for its full
   slow-mode interval)? Likely yes — cheap to add once the gate exists.
4. Should `/status` expose the current gate state (rate, blocked-for) to admins?
5. Since a user session gives higher throughput (their own upload slots and send budget), should
   the bot tell sessionless users that `/login` speeds up large downloads?
