# SpideyBot — Implementation Plan (Kurigram engine)

**Input.** [FUNCTIONAL_SPEC.md](./FUNCTIONAL_SPEC.md) only. This is a **brand-new project**: no code,
layout, naming or behaviour is carried over from any other branch. `V3`/`V4`/`V5`/`main` are out of
bounds as design input.

**Engine.** [Kurigram](https://github.com/kurigram-org/kurigram) `2.2.x` — an actively maintained
Pyrogram fork (import name is still `pyrogram`). Chosen because SpideyBot needs **both** identities in
one process: a Bot-API client *and* per-user MTProto clients (FR-4 message ranges, FR-5 outgoing magic
word, FR-6 `media_unread` inbox). Kurigram supplies both, is async-native, and persists user sessions
as files.

**Status.** Core implementation and the current acceptance/unit suite are complete through P7. The FR-18 resolver currently supports ordinary HTTP redirect shorteners; JavaScript/token/API/Cloudflare-gated families remain explicitly deferred until dedicated adapters are implemented.

---

## 0. Verified environment facts

Checked rather than assumed; these shape the plan.

| Fact | Value | Consequence |
|---|---|---|
| Kurigram release | `2.2.26`, pure-Python wheel, `requires_python >=3.10`, classifiers to **3.14** | Installs on any modern CPython; 3.14 is officially supported |
| `tgcrypto` (the `kurigram[fast]` AES accelerator) | latest `1.2.5`, released **2022-11-11**; no wheel for cp312/cp313/cp314 and no `abi3` wheel; `requires_python ~=3.7`, ships an sdist of **plain C** (not Cython) | pip falls back to a **source build** on 3.12+. Status below |
| `tgcrypto` build on 3.14 | **Verified working**: `cl /c` → `link /DLL` → `import` → IGE and CTR round-trip all succeeded against 3.14.6 + MSVC Build Tools 2026 | Fast AES on 3.14 is viable; no second interpreter needed |
| Kurigram without `tgcrypto` | Verified: logs *"will work the same, but at a much slower speed"* and falls back to pure-Python `pyaes`. **Benchmarked here**: AES-IGE over 8 MiB runs at **171.4 MiB/s** with `tgcrypto` vs **0.59 MiB/s** with `pyaes` — **291× slower** | The fallback is not viable: one 2 GiB transfer would cost ~58 minutes of CPU on AES alone. `tgcrypto` is a hard requirement, not an optimisation, so a missing accelerator must be reported rather than silently tolerated |
| Installed interpreters | only CPython **3.14.6**, plus `uv` | 3.14 is the target runtime |
| `.venv` | **recreated in batch 1**: Python 3.14.6 with the full requirement set, `tgcrypto` compiled from its sdist, plus `ruff` | The planned runtime works; `uv` builds and manages it |
| `.gitignore` | **was deleted during planning, recreated in batch 0**. `git check-ignore -v` now matches both `.env` and `data/` | ✅ Closed — a `git add -A` can no longer stage the bot token, API hash or TeraBox cookies |
| Container runtime | Docker Desktop installed, `com.docker.service` **Stopped** | The compose provider needs Docker Desktop started |
| Native PostgreSQL | none — no install, no service, no `psql` on PATH | The bundled-binaries provider is the no-Docker path |
| `postgresql-binaries` | `18.6.0`, ships **`py3-none-win_amd64`** (Python-agnostic → 3.14 OK); contains `initdb`, `pg_ctl`, `postgres` (9.8 MB), `psql`, `createdb`, `pg_isready` + `LICENSE`; `__init__.py` exposes `bin()` | Powers local Postgres with no Docker and no installer |
| `pgserver` | Windows wheels stop at **cp312**, no sdist | **Rejected** — cannot run on 3.14 (same ceiling as tgcrypto, without the escape hatch) |
| Kurigram client identity parameters | real signature is `device_model`, `system_version`, `app_version`, `lang_code`, `lang_pack`, **`client_platform`** — the docs' sample tables call the last one `platform` | The identity mapper translates `platform` → `client_platform`; an unrecognised platform is dropped rather than raising over a cosmetic field |
| Kurigram smart plugins | `load_plugins()` is invoked from **`start()`**, not `__init__`, and adds each handler through `loop.create_task` | Handlers exist only once a loop has run: a client built and never started advertises zero handlers, and the client captures whichever loop was current when it was constructed |
| Event loop on 3.14 | `pyrogram.utils.get_event_loop()` catches the `RuntimeError` 3.14 raises when no loop is current, then creates one | `client.run()` works on 3.14; `uvloop.install()` must still run first so the new loop comes from the patched policy |
| `logging.LogRecord` on 3.14 | a lone mapping argument stays as `record.args` (the dict itself) and is **no longer merged onto the record** | The redacting filter scrubs that mapping directly; a filter written for the older merge behaviour would miss it |
| Kurigram + tgcrypto on 3.14, live | verified against production: bot connected to `Production DC5`, both handlers loaded, session reported `Device: Desktop - 6.0.2 x64` / `System: Windows 11 x64 (en)` | The identity profile (D16) reaches Telegram for real, not just the config object |
| YouTube format availability | probed against a real video: **0 of 49 formats are single-file** (37 video-only, 12 audio-only, highest video 2160p), and yt-dlp's default selection is the merge `401+251` (AV1 video + Opus audio) | ffmpeg is required for the YouTube feature to *exist*, not to raise its quality. The default pick is also a compatibility trap — see D28 |
| `curl_cffi` streaming | `0.16.3`: `AsyncSession.get(stream=True, impersonate=…)` + `aiter_content()` yield incrementally, and the session is reusable after the response closes. **`chunk_size` is ignored** — it warns *"there is no way to tell curl that"* and yields whatever libcurl read | Read sizes are libcurl's, not ours, so progress is coalesced by our own byte counter rather than by asking for bigger reads. Explicit headers **do** override the impersonation profile (`Accept`, `Accept-Language`, `Referer` all arrive as sent) while the profile still supplies the browser `User-Agent` |
| `curl_cffi` on a truncated body | a body that ends short of its `Content-Length` raises `IncompleteRead` (curl 18), it does not silently yield fewer bytes | A half-downloaded file cannot be mistaken for a whole one; the fetcher only has to discard the partial (E6/E3's integrity) |
| `cyberdrop-dl` CLI | `10.10.0` has a `download` subcommand with `--config-file`, `--output`, `--cookies`, `--dump-json`, and `show` (its own supported-host table). **It exits 0 for an unsupported URL** and writes `unsupported.csv` / `download_errors.csv` beside its log instead. `--cache-file` and `--database-file` are rejected unless the files already exist. A minimal generated config is accepted, and `config new` prints its defaults | The last resort is a subprocess with a generated config, and its **exit code cannot be a success signal** (the same lesson as gallery-dl's exit 0 on a 404, R13): the files on disk are the evidence, and its CSVs are the reason. Generating the config per run keeps each job self-contained, and the log directory is pointed away from the download directory so its bookkeeping is never harvested as a deliverable. D38 |
| ffmpeg on this machine | `9.0.1-essentials` (winget) on `PATH`, under a **version-stamped** directory, with `ffprobe` alongside | Resolve with `shutil.which`, never a fixed path, and never vendor the binaries (licensing + size) |

**Risk R1 (revised after testing — see §8):** fast AES depends on a *source build* (toolchain needed in
CI/Docker) from a package last released in 2022. It builds cleanly on 3.14 today, and `pyaes` remains
the automatic fallback.

---

## 1. Decisions locked (answers to spec §12)

| # | Topic | Decision | Rationale |
|---|---|---|---|
| D1 | Runtime / framework | Python **3.14**, asyncio, single process, **Kurigram 2.2.x**, with a **C toolchain required** (MSVC Build Tools on Windows, a `gcc` stage in Docker) | Kurigram classifies 3.14 as supported, but `tgcrypto` has no wheel above cp311, so 3.14 means compiling it. Chosen over pinning 3.11 — that would only defer the compiler requirement while locking in an older runtime — because the `pyaes` fallback measured 291× slower and so is not a fallback at all |
| D2 | Persistence | **PostgreSQL** — SQLAlchemy 2.0 async + `asyncpg` + Alembic | Enables `FOR UPDATE SKIP LOCKED` leases (FR-10.5) and atomic quota upserts (FR-3.2) |
| D3 | Provisioning | **Bring-your-own-Postgres.** The app never knows where Postgres lives: `[db]` host/port/name/user + `SPIDEY_DB_PASSWORD` is the only coupling. Three equally documented providers — bundled binaries, compose, existing/remote | Direct run without Docker is first-class; Docker is one provider, not a dependency |
| D4 | **No SQLite backend** | Postgres only | The queue depends on `FOR UPDATE SKIP LOCKED`; SQLite would silently change concurrency semantics and break "exactly once" (§8.2/E26) in the environment where it would go unnoticed |
| D5 | Extraction | **gallery-dl is the general extractor**, and yt-dlp is invoked **as a library, for resolution only**, on native video sites (the YouTube family). Native adapters still run first; everything else goes to gallery-dl with `extractor.ytdl.enabled = true`. yt-dlp never transfers bytes for us — resolution hands back per-stream URLs and we fetch them (D25) | **Amended 2026-10 after probing; the original "our code never invokes yt-dlp" was already hollow.** gallery-dl imports yt-dlp lazily, so it is on the path either way, and its `ytdl` extractor hands back a `ytdl:https://…` **marker** in the URL slot (`extension: None`) rather than media — so for exactly those sites the single-surface claim bought nothing and cost clarity. Using `yt_dlp.YoutubeDL(...).extract_info(url, download=False)` directly is the most direct, best-maintained resolution route for YouTube, and the previous implementation already did this (`downloader/site/youtube.py::_fetch_ytdlp_fallback`, branch `V5`). Verified: the resolved pair's per-stream URLs are all present, so D25 still holds. Cross-check — handing D28 to yt-dlp as a format string (`bv*[vcodec^=avc1]+ba[ext=m4a]/bv*[vcodec^=avc1]+ba`) selects `299+140` at 1080p, the same pair `downloader/selection.py` chooses |
| D6 | Adapter scope | **Engine-first.** Engines cover the broad platform list; native adapters only where they fail: **TeraBox**, **Cyberdrop family**, the **YouTube family**, and the **ad-gated hosts** (Doodstream, StreamTape, MixDrop, StreamWish, Vidmonstr, Vidara, Luluvdoo, Bysejikuar, MEGA) | Each native adapter is a module + smoke test + a standing breakage surface (R4); breadth is free from the engines, so reliability is bought only where needed. **Chain order (user decision, 2026-10): native adapter → engine → `cyberdrop-dl` LAST**, only once every other method has failed — it is a heavyweight application with its own state rather than a peer engine, so it is a rescue path. TeraBox keeps its own path. This matches the previous implementation's `downloader/flow.py` (branch `V5`), which falls back native → gallery-dl → cyberdrop-dl, and its `downloader/site/*.py` is the reference for per-site behaviour. YouTube joins the native list because the *engine* cannot satisfy D25 there (it returns a `ytdl:` marker), which is exactly D6's stated test for buying a native adapter. **Amended 2026-10 (D47):** the Cyberdrop family stays on the list of hosts that need their own handling but is *not* one of these modules — V5's `site/cyberdrop.py` wrapped the very program D38 already delegates to, so the family is served by that delegate, and a module for it would be a second name for the same code |
| D7 | **Sessions storage** | Kurigram native **File Storage**, **plaintext**: `data/sessions/<user_id>.session` | Operator decision. Simplest and self-healing; no key management. Accepted risk documented in §8/R10 |
| D8 | **Cookie storage** | Plaintext file: `data/cookies/<user_id>` | Mirrors D7 for consistency. **Both cookie formats are accepted** (2026-10): the engines are handed the file with `--cookies`, which wants Netscape/`cookies.txt`, while the native adapters read the jar themselves — so `sites/bridge.load_cookies` parses Netscape *or* the JSON list the previous implementation wrote. Accepting both means whichever format P7's `/setting` uploads will work, and the failure mode of guessing wrong (no cookies sent, which looks exactly like an expired login) cannot happen |
| D9 | **Encryption** | **None anywhere in the system.** No Fernet, no key management, no `user_secrets` table, and `SPIDEY_SESSION_ENCRYPT_KEY` is dropped | Follows from D7/D8 |
| D10 | Revocation | `/logout` calls `log_out()` **(server-side)** and *then* deletes the file | Deleting only the file leaves the auth key valid and the session listed in Active Sessions — the spec requires genuine revocation |
| D11 | Config layering | Committed `config/default.toml` (all §13 defaults as a reviewable `[tier.*]` table) → optional gitignored `config/local.toml` → `SPIDEY_*` env vars (highest) | Defaults become reviewable in a diff; TOML sources can be validated with `extra="forbid"` so a typo fails loudly instead of silently defaulting |
| D12 | Secrets | Bootstrap secrets only (bot token, API hash, DB password, TeraBox/Reddit creds) via a `SecretsProvider` seam; env today, keyring/Docker secrets later | Keeps non-secret config separable and makes an OS secret store a drop-in |
| D13 | Structure | **Shallow layers**: `main.py`, `app/`, `handlers/`, `downloader/`, `telegram/`; nothing deeper than two levels, no `src/` nesting | Operator decision. The package is `downloader/`, not `download/`, so the code that *downloads* is never confused with `data/downloads/`, where the bytes land — and it matches the previous implementation's naming |
| D14 | Handler registration | Kurigram **smart plugins** — `plugins=dict(root="handlers", include=[...])` with `@Client.on_message` decorators | Removes the manual registration layer entirely. Caveat: one handler per group per update, so commands, the magic word and TimerMedia need separate groups |
| D15 | Queue / workers | Single process; Postgres priority queue with leases, written for `SKIP LOCKED` from day one | Multi-worker later becomes a config change, not a rewrite |
| D16 | Client identity | Configurable `[client] profile = "desktop" \| "android" \| "custom"` for **user clients** (`device_model`, `system_version`, `app_version`, `lang_pack`, `lang_code`, `client_platform`) | Account-safety, not cosmetics: a "CPython / Kurigram" identity in a user's Active Sessions raises the odds of their account being actioned |
| D17 | Speedups | `tgcrypto` installed unconditionally; `uvloop` a guarded **Unix-only** optional install | `uvloop`'s own wheel markers exclude Windows, so Windows dev runs without it |
| D18 | Scheduling | Hand-rolled asyncio scheduler with an **injected deterministic clock**; APScheduler not adopted | The test plan needs a controllable clock for the daily reset and 30-day bonus window |
| D19 | Live testing | Opt-in `@pytest.mark.live` suite driven by Kurigram **`test_mode=True`** test servers | Exercises login, 2FA, session lifecycle and triggers against test DCs with throwaway accounts |
| D20 | Admin elevation | Command-only (`/addpremium`, `/removepremium`, `/checkpremium`) with inline confirmations; grant rows carry expiry | FR-19, FR-2.2 |
| D21 | Cleanup / retention | Per-job temp dir removed on every terminal state; startup orphan sweep; expired leases re-queued; idle user clients reaped (idle timeout + LRU + live cap); retention window for history | FR-10.5, E26, NFR-3 |
| D22 | Rate limiting | Per-user submission token bucket; `FloodWait` → sleep + single retry (reporting seconds during `/login`); per-host exponential backoff with jitter | FR-7.7, E8, E14, NFR-2 |
| D23 | Localization | Not in v1. All user-facing copy in one catalog module keyed by id, preserving spec §7 wording | Later i18n becomes a data change |
| D24 | File transfer | Stream to a per-job temp dir, upload with progress callbacks, group ≤ `album_size` (10) per media group, flush remainder, delete the temp dir on completion | FR-8, NFR-3 |
| D25 | Byte transfer | **We fetch the bytes ourselves** (`curl_cffi`/`aiohttp`) from the media URLs gallery-dl resolves, rather than letting gallery-dl's downloader do it | Uniform byte-level progress for FR-7, our own size accounting for the quota, and the E3 pre-download size gate operates on resolved sizes. Costs are tracked in R13 |
| D26 | Extraction config | A committed `config/gallery-dl.json` enables `extractor.ytdl` and pins `extractor.ytdl.websites` as an allowlist, so the generic yt-dlp fallback cannot swallow URLs our native adapters should own | Enabling ytdl makes the generic extractor match *any* otherwise-unhandled URL; the allowlist keeps that from pre-empting TeraBox and the ad-gated hosts |
| D27 | Required capabilities | `tgcrypto`, `ffmpeg` and `ffprobe` are **required capabilities**: probed at startup (a missing one is logged at ERROR), exposed by `spideybot check` (non-zero exit), and consulted by the code that needs them so a job fails with a reason rather than degrading | Starting silently is exactly what makes a 291× slowdown, or a download with no audio, hard to diagnose. Chosen over refusing to start because hosts that need neither still work |
| D28 | Media selection | Compatibility-first: prefer H.264 + AAC in MP4, using the best available stream (AV1/Opus) only when nothing compatible exists. **Settled 2026-10 as A+C: compatibility-first by default, with the choice exposed as `[download] video_preference = "compatible" \| "quality"`.** Chosen while the user was unavailable, on the grounds that it satisfies the spec's "use AV1/Opus only when nothing compatible exists", keeps the tier table about limits rather than quality, and is reversible by changing one config value | A file that will not play is not a successful download: AV1 in Telegram is client-dependent, while H.264/AAC in MP4 is universally playable and merges by stream copy (no transcode). Measured on the probed video: compat `299+140` = **1080p, ~255 MB**; yt-dlp's own default `401+251` = **2160p, ~689 MB**. The size is the stronger argument — the daily bandwidth cap, not the download count, is what really limits a free user (~4 downloads/day at 255 MB against ~1.5 at 689 MB), and 689 MB exceeds a free user's 500 MB link limit outright. A stream we cannot play is still delivered, as a document with the reason in the caption, rather than failed |
| D29 | Upgrade button | `[ux] upgrade_url` is operator-supplied and empty by default; while it is unset the button is omitted rather than pointing somewhere invented | AC-1 requires the button but no destination appears anywhere in the spec. Validated at startup to be a link Telegram accepts (http/https/tg) so a typo fails immediately instead of as an API error on the first `/start`. A button-free welcome still satisfies the rest of AC-1 |
| D30 | Resolver output | **A file to deliver is one *or more* transfers**, and sizes are all-or-nothing: a total is reported only when every stream reported one | Every YouTube format is a merge (measured: 0 of 53 carry audio), so "one file = one URL" cannot express what a job actually does — and the fetcher needs per-stream URLs to report byte progress, while the size gate needs the size of the *result* rather than of whichever stream was counted first. All-or-nothing exists because a partial sum looks precise: `None` for an unknown size must never read as `0`, or the gate waves through a download it cannot measure (E3/AC-3), while a reported `0` is a real measurement and E6 has a specific message for it |
| D31 | Size gate on an unknown size | An unreported size **passes, flagged for enforcement during transfer** — the fetcher aborts if the running total passes the cap | Refusing would break links that would have worked, since many extractors publish no size; allowing it silently would let an oversized share through and spend the user's daily quota discovering that. Flagging keeps one rule (the limit holds) and enforces it where it can be. `limit_mb = 0` means unlimited, so there is no cap to enforce |
| D32 | Request headers | Browser defaults plus per-host requirements live **in code** (`downloader/fetcher.py`), not in config. The `User-Agent` is not ours at all: `impersonate="chrome"` supplies a real browser's, while our explicit headers override the rest of the profile (measured) | A `Referer` a host demands is a fact about that host, so a config key would only offer a way to get it wrong; and the media host is not the page host, which is why `googlevideo.com` carries the YouTube referer. Cookies are the deliberate exception, because those belong to a user and live in their own file (D8) |
| D33 | No Range, no resume | A transfer always starts from byte zero. A job whose lease expired simply fetches again | The spec never asks for resume, and the ledger that makes a restart safe (`job_items`) exists to prevent re-*delivery*, not re-download. A resumed partial is the riskier design: nothing verifies that the bytes already on disk are the ones the server would still send, so a wrong assumption yields a corrupt file delivered as a success. Measured consequence: curl raises `IncompleteRead` on a short body, so a partial never survives as a file either |
| D34 | The chain's outcome vocabulary | A resolver returns a `Resolution` (possibly empty) or raises `ResolveError`, and **each attempt's outcome is machine-readable**: `ResolveError.empty` separates "found no media" (E4) from "the extractor failed" (E2). Every attempt that yields no files continues the chain; if none ever reported a failure the result is an **empty share**, otherwise the last failure's reason is reported | Both kinds of nothing mean "this resolver cannot help", so both must move to the next one — but they are two different messages: E4 is `No actual files found in this share` while E2 repeats the extractor's own reason. Deciding that from the reason's *text* would have worked until the first wording change, so `parse_gallerydl` returns a `ParseResult` carrying `empty` instead of flattening both cases into one string |
| D35 | Byte accounting | `record_bytes(inbound=…)` is charged for the bytes **actually pulled off the network**, including a partial that was discarded, and is recorded after the fact rather than reserved up front. `download_count` is charged per job at submission, as already specified | The bandwidth cap is measured against `usage.bytes_in`, and a failed or cancelled transfer really did consume that traffic. Nothing is reserved in advance, so counting the partial cannot double-charge; and charging what the network actually moved is the only number that stays true when a host's reported size was wrong |
| D36 | Cancellation reaches the transfer | FR-11.6 is achieved by polling `should_stop` **between chunks**, raising `TransferCancelled`. A cancelled transfer is not a failure: it is not listed in FR-9's failures, and the partial is discarded | Checking only between files would leave a cancel looking ignored for as long as the current file takes — minutes, for a large video. `TransferCancelled` subclasses `FetchError` so the existing discard paths still run, but is caught separately so a user's own cancel never appears as something that "went wrong" |
| D37 | Filenames inside a job | A share's files are staged under `unique_name(safe_filename(title))`, numbering from `(2)` on collision; a number is spent only when the file actually lands | Two files in one share can carry the same title, and without this the second silently overwrites the first: the user is told two files were downloaded and receives one. Releasing the name on failure means a transient failure does not permanently rename the file that succeeds on the retry path |
| D38 | The last resort delegates | cyberdrop-dl **downloads for us** rather than resolving, so the pipeline has two fetch strategies: the chain (resolve → our own transfers) and, when the chain fails, a delegating fetch that hands back finished files. It cannot sit in the chain, and it is not gated on any host list. What this costs: **the pre-transfer size gate is unavailable** — a CLI cannot say what it is about to fetch — so AC-3 is enforced by D31's running cap, measured against the bytes on disk, and the run is killed when they pass the limit. Its own CSVs (`unsupported.csv`, `download_errors.csv`) supply the failure reason, because **its exit code is 0 even for an unsupported URL** (measured) | Every other path can be measured before it runs, so every other path keeps AC-3's "refuse before a single byte moves". This one cannot, and pretending otherwise would mean either refusing links it could have served or letting a user's bandwidth cap be passed silently. Running it *last*, only after every measured method has failed, is what makes the trade acceptable: without it the choice is between an unmeasured attempt and no attempt at all. Config is generated per run and its keys are validated against the tool's own defaults in the tests, because a typo in a generated config is a silent feature loss. Logs are written outside the download directory, since the tool puts its CSVs there and they are not deliverables. V5's hand-written host list is deliberately not carried over: the tool knows its own hosts, and a copy would only go stale |
| D39 | The chain is composed by the registry, not by the pipeline | `downloader/registry.py` owns host knowledge and builds the chain; `flow.download_link` receives it. The order is native adapter (if one exists) → yt-dlp for the video hosts it handles natively (D5) → gallery-dl → the delegate (D38). Cookie files are passed through, never looked up | The pipeline stays host-free, so adding an adapter never touches the fetcher, the gate or the copy. Per-user cookies are a caller concern (D8); a registry that read cookie files would be a second place that knows where they live |
| D40 | The job handler is thin, and delivery knows no database | `app/jobs.py` joins pipeline → ledger → delivery → message and owns only bookkeeping; `telegram/delivery.py` takes finished files and a chat and never touches the DB; `flow.LinkResult` supplies everything the messages say | The three parts can each be tested where they live: the pipeline without Telegram, delivery without a job, and the wiring against a recording client with a real database. It also means the wording stays in one place (D23) — `final_message` returns text rather than sending it, so the copy is testable on its own |
| D41 | The ledger key is the resolved media URL | `job_items.source_url` is the *resolved, fetchable* URL of the file, never the page the user pasted, and a file already recorded as `uploaded` is not sent again on a later attempt. `FetchedFile` therefore carries both URLs: `source_url` (the pasted link, for the caption) and `resolved_url` (the ledger key). A delegated file, which has no resolved URL at all, uses a `file:` URI of its own name | The schema's `(job_id, source_url)` constraint is what makes re-delivery impossible, and one pasted album link yields many files that *share* the page URL — so using the page URL as the key would either collide on the constraint or, worse, mark a whole album as delivered when one file was. Known limitation: a time-limited media URL (R13) changes between attempts, so a job resumed after resolution can re-deliver; the job-level guard (`status = delivered`) is what covers the ordinary case, and the item ledger covers the rest |
| D42 | A deterministic outcome is not retried | `workers.JobOutcome.retry` is false for AC-3's refusal, E4's empty share and a cancellation, and true for a file that failed | Retrying is right for a host that hiccupped and wrong for a verdict that cannot change: the link stays over the limit, and the share stays empty. Without this, one oversize link would spend three attempts, three messages and three downloads from the user's daily allowance discovering the same fact |
| D43 | **HLS is a transfer role, not a file** | `TransferRole` gains `"hls"`. An `.m3u8` transfer is consumed by ffmpeg (`-i <playlist> -c copy -f <container>`), with progress and D31's cap taken from the bytes appearing on disk by polling, and with the child process *killed* on cancel or cap breach. The transport is decided **per transfer** (`fetcher._fetch_part`), so a merge can have a playlist as one of its halves. The plain download path additionally refuses a response whose body starts with `#EXTM3U` | Three ported adapters (StreamWish, Luluvdoo, Bysejikuar) hand back a playlist URL and their docstrings claimed "yt-dlp handles HLS", which nothing did — the fetcher had no content-type check, so it would have written the playlist *text* to disk and delivered it as a `.mp4`. A playlist is a manifest, not media, which is the same category error R13 records for yt-dlp's `ytdl:` marker. The body check exists because the role can only be set by a resolver: a bridge can answer with a fresh `.m3u8` URL under a name no resolver classified, and the fetcher is the only component that sees the bytes. **Amended the same day, from a live run:** a Reddit video resolves through yt-dlp to `v.redd.it/…/CMAF_720.m3u8` **plus** `…/CMAF_AUDIO_128.mp4`, so `probe` now marks a playlist-shaped stream `hls` whatever it carries, and dispatching on "the item contains an hls transfer" (the first implementation) silently dropped the audio — 658 KB of silent video, measured, where the merge produces 1.25 MB with both streams. Two further details came from the same run: the output container is passed to ffmpeg explicitly, because a part file's `.part0` suffix is not a muxer ffmpeg will accept, and an `hls` transfer counts as the video half of a merge. An HLS stream has no `Content-Length`, so its row reports bytes without a percentage — a deliberate absence rather than an invented number (D30) |
| D44 | **Native breadth comes from the bridge pattern, and the table** | 24 of V5's 26 site modules are ported and **25 native adapters are wired** (`cyberdrop` is not a port — D47 — and `bluesky` was removed after measurement — D49 — while `terabox`, which V5 does not have, is wired too, so the counts do not line up one-to-one by design). **Twelve** of them are "hand the link to a third-party saver endpoint and read the URL out of the answer", so that plumbing lives once in `downloader/sites/bridge.py`; the rest keep their own parsing. `registry.chain_for` composes from a `_NATIVE_TABLE` of `(module, arguments, options scope)` rows rather than an `elif` ladder | D6 was written when the engine-first claim looked stronger than it turned out to be: gallery-dl covers the *mainstream* platforms, but the previous implementation's per-site list existed for the ad-gated and bridge-only hosts, and the operator's own experience is that gallery-dl alone misses them. Twenty-five `elif` branches would be a place where one wrong branch silently hides every host below it, so the chain is data now — with tests asserting that no two rows claim a host and that every module in the package is wired. Bridge plumbing is shared for the same reason R4 gives: each adapter is a standing breakage surface, so the part that is identical everywhere should be small and tested once. Every adapter also takes `client=` (an injectable session), which is what makes it testable at all — ten of the ports did not, and the untestability is exactly why a fatal bug in one of them stayed hidden (D47) |
| D45 | **Adapter options are scoped, and credentials travel as CLI options** | `ChainContext.options` is a mapping keyed by the strategy's own name (`reddit`, `gallery-dl`); the registry hands each adapter only its own slice. Reddit's credentials come from `settings.reddit_praw_client_id/secret` and, optionally, `settings.reddit_praw_refresh_token`; gallery-dl's Reddit credentials from `settings.gdl_reddit_*` as `-o extractor.reddit.client-id=…` | The seam already existed for this and was read by nobody, while `config.py` declared four Reddit settings that nothing consumed — a dead setting is worse than an absent one, because it looks configured. `-o` rather than D26's committed `config/gallery-dl.json`, for the reason the ytdl flag is passed that way: an option cannot be shadowed by the operator's own gallery-dl configuration. Scoping keeps the registry free of per-site knowledge in both directions |
| D46 | **Reddit authenticates through V5's identity chain, and validates each identity** | `downloader/sites/reddit.py` builds a PRAW client from: a configured refresh token (an account-level login) → configured client credentials → Reddit's **public installed-app pairs** (V5's three, kept verbatim). Each candidate is proved with a real request (`auth.scopes()`), so a refused one falls through to the next. The blocking authentication runs in a worker thread (`asyncio.to_thread`), as PRAW is synchronous | The public pairs are what make Reddit work on a checkout with no app registered — the operator's instruction was to keep using them. My first port *named* them but never validated, which made them decorative: PRAW authenticates lazily, so the first pair always "won" and a dead one surfaced later as "Reddit would not answer" instead of being skipped. Measured live 2026-10: pairs 1 and 2 authenticate (scopes `{'*'}`), pair 3 answers **401**; a real r/aww photo and video both resolve through the adapter, and the video downloads to a playable 1.25 MB file with both streams |

| D47 | **The Cyberdrop adapter is not a port, and every wired adapter is called by a test** | `downloader/sites/cyberdrop.py` is deleted and unwired; the Cyberdrop family is served by the `cyberdrop-dl` delegate (D38/D6). `tests/unit/test_registry.py` gains `test_every_wired_adapter_fails_cleanly`, parametrised over `_NATIVE_TABLE` itself, which calls each adapter with the arguments the registry passes and asserts it raises `ResolveError` and nothing else; every adapter gained a `client=` parameter so that call is possible. A second assertion in the same test — that the injected session was actually used — catches an adapter that ignores the switch's client and reaches the network on its own | Found by running the smoke matrix: every Cyberdrop link produced `TypeError: DelegatingFetch.__init__() got an unexpected keyword argument 'engine'`. That port wrote `raise DelegatingFetch(...)` — a frozen `(name, run)` dataclass, not an exception, and `DelegatingFetch` is a *fetch strategy*, which a resolver cannot be (D38). The chain catches `ResolveError`, so a `TypeError` escapes it and kills the whole job instead of falling through to the engines. It survived review because no test had ever *called* an adapter: the registry's tests only inspected chain names, and ten adapters did not accept an injected session, so calling one meant hitting the real site. That is the general lesson, recorded as the rule rather than the incident: a wiring test that only reads the table cannot see a module that crashes when used |
| D48 | **Kuaishou resolves from its own share page** | `downloader/sites/kuaishou.py` fetches `https://v.m.chenzhongtech.com/fw/photo/<id>` with a **mobile** user agent, reads the `window.INIT_STATE` JSON out of the markup, and takes the highest-bitrate non-hidden `representation` URL from `photo.manifest.adaptationSet` (with the `mainMvUrls` mirrors as the fallback when a post has no manifest). A `v.kuaishou.com/<code>` link is followed first (`bridge.get_final_url`) because the photo id exists only in the redirect's destination. The caption names the file, and the manifest's `fileSize` becomes the known size the pre-transfer gate needs. No cookies and no third-party bridge | The bridge this port started with is dead (R17), and the two obvious replacements are both closed: Kuaishou's desktop GraphQL answers `result: 400002` with a **sliding-captcha challenge** once a real `did` cookie is present (measured: a synthetic `did` gets a bare `result: 2`, the site's own `did` gets the captcha), and the mobile `rest/wd/photo/info` endpoint answers `result: 2` for both web and Android `did` formats. The share page was the third path, and it works. Two traps cost real time and are now stated in the module: the state serialises booleans as **strings** (`"False"` is truthy), and it only embeds the state for a mobile user agent — the desktop page is a 74 KB JavaScript shell against the mobile page's 162 KB. Verified live for both link forms: `…/short-video/3xqqmetbyqh4qie` and `v.kuaishou.com/2xZgBx` resolve to the same post's 720p AVC file, 3,784,687 bytes, with the candidate URLs confirmed fetchable by ranged GET (a plain HEAD 404s on some of that CDN's hosts, so HEAD is not the test) |
| D49 | **Bluesky is left to gallery-dl, and the removal is the evidence** | `downloader/sites/bluesky.py` is deleted and unwired (`downloader/sites/__init__.py` records why), so `bsky.app` links fall through to gallery-dl, which handles them properly. The same measured-and-decide rule as D47: an adapter that cannot resolve is a wasted attempt with a nicer error message | Measured live 2026-10 against **four real posts** (two video, two image, including the one gallery-dl is known to handle): the bridge's answer carries **no result section** for any of them — not a rate limit, not a stale payload shape, the endpoint simply no longer serves real posts. gallery-dl, run on the same links, returns records with `meta.extension: mp4` and a blob URL that fetches `200 video/mp4`. Had the adapter stayed, every Bluesky link would have burned a bridge call, printed a fallback notice, and then worked — the worst kind of green-ish behaviour, because the notice says something is broken when nothing is. This is the second removal of the same shape as D47's (cyberdrop), and both point the same way: an adapter has to be *measured* against a live link before it is trusted, which is why §5's live matrix is a step rather than an assumption |

| D50 | **A user session is a budget, and it is proved before it is used** | `telegram/sessions.py` owns the pool: a live cap that evicts the least-recently-used *idle* client and refuses when every live client is busy, a per-conversation login timer that closes an abandoned client, and a reconnect that asks `get_me()` before trusting a client. A session Telegram has revoked is **deleted**, and the badge says `🔴 No session` | Three separate limits, because they answer three different ways of running out: sockets (the cap, R3), time (the idle timeout, enforced by `scheduler.tick`), and account safety (one session per user, FR-13). `connect()` returning is not evidence a key still works — a revoked session often connects and then refuses to answer — so every reconnect makes one request, the same eager-validation rule as D46. A file whose key is dead is worse than no file: `/account` would advertise a session that can never connect, and the state machine in §8.3 has only three states, none of which is "stored but unusable" |
| D51 | **`/dt` runs through the same queue as `/dl`** | `handlers/telegram_dl.py` parses and checks the session, then calls `handlers/submission.submit` — the same admission path as `/dl` (FR-3). The job handler branches on the link: a Telegram link is fetched by `telegram/media.fetch` with the *user's* session, everything else by the pipeline, and both produce a `flow.LinkResult` so the ledger, the gate, the albums and the final message are shared | A `/dt` that bypassed the queue would be a second implementation of quota, concurrency and cancellation — and the one thing the spec is explicit about (FR-10.2) is that downloads share one queue. It also made the status message free: the job carries `status_message_id` from `/dt` exactly as from `/dl`, so FR-7's live view works on Telegram jobs without knowing they are different. The branch is on the *link* rather than a stored flag, so a job row survives a restart with nothing to fall out of step |
| D52 | **A folder link downloads one message per chat** | `t.me/addlist/<slug>` parses into `FolderLink`; the resolver reads the filter through the user's session, and for each chat scans up to `FOLDER_SCAN` (10) recent messages and takes the first with media | FR-4.3 asks for folder support and a folder names *no content*: it is a filter over chats. "Download the folder" is therefore unbounded — it could quietly spend a whole allowance on one click, and no per-link limit can refuse something whose size is unknown until it has been fetched (the opposite of AC-3's rule). One message per chat is bounded, predictable, and reported in FR-4.5's counts (chats scanned), so the user can see exactly what it did. Reconciling that against the spec's wording is an operator decision, not a silent invention |
| D53 | **The size gate runs on Telegram's own metadata** | `telegram/media.fetch` sums `file_size` from the messages and calls `gate.check_size` **before** requesting anything, and the AC-3 refusal is returned as the same `SizeCheck` the HTTP path returns | Telegram reports real sizes for everything it stores, so unlike an extractor's guess this number is trustworthy — which makes AC-3's "refuse before a byte moves" *stronger* on this path than on the HTTP one, where an unreported size can only be flagged for enforcement during transfer (D31). Returning the gate's own verdict rather than a sentence keeps one refusal message for every source |
| D54 | **The two reply features live on the user's client, and speak only in Saved Messages** | `telegram/magic.py` and `telegram/timer.py` register `MessageHandler`s on the client the pool connects — before `initialize()`, or they would miss whatever arrives in between — and both file what they save in Saved Messages. The magic word is *compared* per message rather than expressed as a filter | FR-5.3 and FR-6.2 are about the user's own account (their outgoing messages, their incoming private media) and Telegram only sends an account its own updates, so a handler on the bot client would see the wrong half of every conversation. Saved Messages is the only chat either feature may write to: the command may have been typed in someone else's chat, and a bot that answered there — as the user — would do something with consequences the user cannot undo. A filter built from the magic word would have to be rebuilt every time the word changed (FR-14), and a handler that is replaced is one that can be forgotten; one case-insensitive comparison per outgoing message costs nothing |
| D55 | **`media_unread` is implemented as "arrived while live, and not already saved"** | FR-6.2's unread rule is enforced by Kurigram's `skip_updates` default (on), so media from before this session is never dispatched rather than merely filtered, plus a bounded per-process set of `(chat, message)` pairs for the repeated-update case a reconnect can cause. The residual gap is written down in the module rather than assumed away: media that arrives *while connected* and is then read on another device will still be saved | Pyrogram exposes no `media_unread` field on `Message` (Telethon does, which is why the spec names it), and reading it would mean a `RawUpdateHandler` and a second parser for the same event. What the spec actually protects is the offline backlog of media the user has already seen, and `skip_updates` makes that invisible at the source — a stronger guarantee than filtering it out afterwards. Recording the gap is the point: an approximation that is written down can be revisited; one that is silently treated as exact cannot |

---

### 2.1 Process model

```
                 ┌──────────────────────────── asyncio process ────────────────────────────┐
                 │                                                                         │
  Bot API ──────▶│  BotClient (kurigram, bot_token)                                        │
                 │      │  on_message / on_callback_query                                   │
                 │      ▼                                                                  │
                 │  handlers/ (smart plugins)  ──▶  app/ (tiers·quota·referral)             │
                 │      │                                 │                                │
                 │      │  enqueue                        ▼                                │
                 │      └──────────▶  app/queue (Postgres, priority + lease)                │
                 │                           │                                             │
                 │  user clients (session files) ◀── workers (N asyncio tasks)              │
                 │      ▲   ▲                │                                             │
                 │      │   │                ▼                                             │
                 │  handlers/  │  downloader/flow ──▶ engines + sites + bypass             │
                 │  magic_word │             │                                             │
                 │  timer_media│             │                                             │
                 │             └─────────────┴──▶ telegram/delivery → albums → user chat    │
                 │                                                                         │
                 │  app/scheduler  (daily reset · lease sweep · temp sweep · client reaping)│
                 └─────────────────────────────────────────────────────────────────────────┘
                          │                                  │
                 PostgreSQL (users·quota·jobs·                temp files under
                 referrals·prefs·stats)                       data/tmp/<job_id>/
                 data/sessions/ · data/cookies/ (plaintext files)
```

### 2.2 Package layout

Nothing deeper than two levels; no `src/` nesting.

```
SpideyBot/
  main.py                  # entrypoint: Client(...) + plugins=dict(root="handlers")
  app/                     # infrastructure + domain
    config.py  logging.py  paths.py
    db.py  models.py  bot.py  runtime.py
    tiers.py  quota.py  queue.py  scheduler.py
    sessions.py  cookies.py  referral.py  metrics.py
  handlers/                # Kurigram smart plugins, decorator-registered
    start.py  help.py  download.py  telegram_dl.py  cancel.py  job_status.py
    account.py  login.py  setting.py  quota_info.py  sites.py  referral.py
    bypass.py  admin.py  magic_word.py  timer_media.py
    submission.py          # the admission path /dl and /dt share (FR-3)
  downloader/              # the code that downloads; the bytes land in data/downloads/
    engines.py  flow.py  probe.py  registry.py  selection.py
    sites/                 # native adapters only: terabox.py, youtube.py, the ad-gated hosts,
                           # the bridge-backed platforms, and the two that read their own site
    bypass/                # FR-18 shortener families
  telegram/
    clients.py  sessions.py  login.py  links.py  media.py  magic.py  timer.py
    status.py  delivery.py  progress.py  keyboards.py
  tests/                   # unit/, integration/, acceptance/, fakes/
  migrations/              # Alembic
  config/                  # default.toml (committed), local.toml (gitignored)
  data/                    # runtime, gitignored: pgdata/, sessions/, cookies/, tmp/, downloads/
  pyproject.toml  requirements.txt  .env.example  .gitignore
  Dockerfile  docker-compose.yml
```

Handler filenames deliberately avoid colliding with domain modules: the `/status` command is
`handlers/job_status.py` (the status-message view is `telegram/status.py`) and `/quota` is
`handlers/quota_info.py` (the engine is `app/quota.py`). Without that, a search for `status` or `quota`
returns two unrelated files.

**Boundary rule:** handlers never touch the DB or the extractors directly. They call `app/` facades and
enqueue jobs; the pipeline is reachable only through `app/queue`. This is what makes AC-1…AC-15
testable without a live Telegram connection.

### 2.3 Kurigram-specific design notes

| Concern | Approach |
|---|---|
| Bot identity | `Client(name, api_id, api_hash, bot_token=…)`; handlers via `@Client.on_message(...)` / `@Client.on_callback_query(...)` |
| Per-user identity | `Client(f"data/sessions/{user_id}", api_id, api_hash, …)` — file storage, so restart reconnects with no login |
| Command parsing | `filters.command(..., prefixes="/", case_sensitive=True)` gives whole-word matching **and** the `/cmd@BotName` form for free (§4 parsing rules) |
| `/start <payload>` | `message.command[1]`; absent/non-numeric/self payloads ignored silently (FR-1.2, E21, E22) |
| Status edits | `edit_text`; `MessageIdInvalid` → recreate the message (E7); `FloodWait` → sleep + retry, coalesced by `telegram/status.py` (FR-7.7, E8) |
| Albums | `InputMediaPhoto/Video/Audio/Document` lists → `reply_media_group`; caption on the first item (FR-8) |
| Progress | `progress=` callbacks on download/send calls → `telegram/progress.py` → status rows (FR-7.4/7.5) |
| `/dt` ranges | `get_chat_history` over the parsed id range; counts scanned messages, reports source chat title (FR-4.5) |
| Magic word | user-client handler on `filters.private & filters.outgoing`, single-token match, own group (FR-5.3, D14) |
| TimerMedia | user-client handler on `filters.private & filters.incoming` requiring photo/video + `media_unread`, own group (FR-6.2, D14) |
| Login flow | phone → code → optional 2FA on a pending user client; `SessionPasswordNeeded`, `PhoneCodeInvalid`, `PhoneCodeExpired`, `FloodWait` mapped to spec §7.6 copy |
| Ban on old engines | Importing `telethon` or `cryptg` anywhere is a lint failure — they must never leak in |
| Support modules | `python-socks[asyncio]` and `pyaes` arrive with Kurigram; `tgcrypto` is **required**, not optional (D1) |
| Runtime plugin control | Each decorated handler exposes `func.handlers` as `(handler, group)` tuples, so `remove_handler()` can unload a plugin without a restart — the basis for a maintenance kill-switch (e.g. disabling `/bypass` during an incident) |
| Session escape hatch | `export_session_string()` is held in reserve: it is how a session would be backed up, migrated between hosts, or moved to a DB-backed store if R11 ever bites |
| Test-mode namespace | Live tests use `data/sessions/test/<user_id>.session`, so a test run can never clobber a real user's session |

### 2.4 Adopted from the Kurigram documentation

| Doc page | What we adopt |
|---|---|
| client-settings / decreasing ban | The verbatim presets in §6 are used for user clients (D16). Two cautions from the page: **do not ship Telegram's own app credentials** (`api_id=6` / `2040` in their snippets belong to Telegram's official clients) — we keep the operator's `my.telegram.org` pair; and their `lang_code="jabka"` is a placeholder, not a real ISO 639-1 code, so we default to `en` |
| speedups | `tgcrypto` is **required** and checked at startup, because a missing accelerator is 291× slower (D1/R1); `uvloop` guarded and Unix-only (D17). **Ordering matters:** `uvloop.install()` must run before `asyncio.run()` *and* before any `Client` is constructed, or it silently has no effect — done in `main.py` inside `try/except ImportError` |
| smart-plugins | Decorator-based registration from `handlers/` with an explicit `include` list for deterministic order (D14); per-module order can be pinned with the `"module fn3 fn1 fn2"` syntax; runtime unload via `remove_handler()` (§2.3). Overlapping filters must sit in different groups — "one handler per group, per update" is enforced by design |
| storage-engines | Native File Storage for sessions (D7). `in_memory=True` is used for throwaway clients so the live suite leaves no session files behind; `export_session_string()` is held in reserve for backup/migration (§2.3); the `Storage` ABC is the documented extension point should files ever need encrypting |
| scheduling | APScheduler available but not adopted; a hand-rolled scheduler keeps the injectable clock (D18) |
| test-servers | `test_mode=True` with reserved numbers `99966XYYYY` (X = DC 1–3) whose **login code is computable** — the DC digit repeated five or six times — so the login flow can be automated with no phone. Running **two** test-account clients against each other makes AC-7 fully automatable on test DCs (message, channel and range links with captions), because the chat content is ours to create (D19) |

---

## 3. Data model & on-disk state

PostgreSQL holds everything except credentials, which are plaintext files (D7/D8).

```
users(id PK, username, first_name, tier, tier_expiry, is_admin,
      onboarded_at,            -- FR-1.5/1.6 "already started", survives restarts
      referred_by,             -- denormalised for first-start-wins
      created_at, updated_at)

tier_grants(id, user_id FK, tier, granted_by, granted_at, expires_at, revoked_at)   -- FR-2.2, FR-19

usage_daily(user_id, day, downloads, bonus_downloads, bytes_in, bytes_out,
            PRIMARY KEY(user_id, day))                                                -- FR-15, §8.5

jobs(id BIGSERIAL, user_id FK, link, site, tier, priority, status, route,
     attempts, total_bytes, resolved_count,
     claim_token UUID, lease_expires,            -- restart-safe lease (FR-10.5)
     status_chat_id, status_message_id,          -- the single status message (FR-7.1)
     created_at, started_at, finished_at, cancel_requested_at)

job_items(id BIGSERIAL, job_id FK, idx, source_url, filename, media_kind,
          bytes_total, bytes_done, state, reason, uploaded_message_id,
          UNIQUE(job_id, idx), UNIQUE(job_id, source_url))                        -- FR-8.5, FR-9, E6

referrals(id, referrer_id FK, invitee_id FK UNIQUE, status, credited_at, created_at)  -- FR-17.6
referral_bonuses(id, referrer_id, invitee_id, credited_at, expires_at)                -- +10/day for 30d

user_prefs(user_id PK, magic_word, timer_media, locale, updated_at)                   -- FR-5.2, FR-6.4

stats_daily(day, submitted, delivered, failed, cancelled, files_sent, bytes_up)       -- FR-19 /stats
```

**No `user_secrets` table** (D9). On-disk instead:

```
data/sessions/<user_id>.session    # Kurigram SQLite session, plaintext (D7)
data/sessions/test/<user_id>.session  # live-test sessions, kept separate so a test run
                                      # can never clobber a real user's session (D19)
data/cookies/<user_id>             # TeraBox cookie, plaintext (D8)
data/pgdata/                       # local Postgres cluster, bundled-binaries provider
data/tmp/<job_id>/                 # per-job staging, deleted on terminal state
```

Session state for FR-13 badges is derived from file presence plus the live-client registry: no file →
🔴 No session; file present, client stopped → 🟡 Session saved (not running); live client → 🟢 Connected.

Design notes:

- **One status message per job** is a *stored* pair (`status_chat_id`, `status_message_id`) so a
  restarted worker rebinds to the same message instead of spamming a new one (FR-7.1, E7).
- **`job_items` is the idempotency ledger** (FR-8.5, FR-9.1, E26): `state ∈
  pending|downloaded|uploaded|failed`, and `files_sent` derives from it, so a resumed job never
  re-uploads what already landed.
- **Quota is one row per user-day** → atomic `INSERT … ON CONFLICT DO UPDATE` (FR-3.2, §8.5).
- **Leases, not locks held across awaits** → claim, renew, requeue on expiry (D15, E26).

---

## 4. Phases

Each phase is independently shippable with a *done when* that a command can verify. AC ids are spec §11.

### P0-prereq — DB provider (Docker-free)
- [x] `app/paths.py`; `spideybot db` CLI: `init | start | stop | status | migrate | reset` over
      `postgresql-binaries` — exercised: `init`, `start`, `status`, `migrate`. `reset` is implemented
      but deliberately not exercised, since it destroys the cluster
- [x] Loopback-only, non-default port, `data/pgdata/` gitignored, idempotent `start`, graceful stop
- [ ] Layered test DSN resolver: `SPIDEY_TEST_DATABASE_URL` → bundled cluster → compose service

**Done when:** `python main.py db start` boots Postgres with Docker stopped, and the integration
suite passes both with and without Docker. — **Half met:** the DB half is verified (Docker stopped,
`initialised/running/ready`, migrations applied and idempotent). The integration suite arrives with
the queue in batch 2, since there is nothing to integrate until then.

### P0 — Foundations
- [x] Recreate `.gitignore` **first** (`.env` is currently unignored — see §0)
- [x] Fresh Python 3.14 venv via `uv`; `uv pip install -r requirements-dev.txt` (runtime dependencies plus
      pytest and ruff — split so the image installs neither).
      `cryptography` is absent because D9 encrypts nothing, and `camoufox` is deferred to batch 5 per
      §12. `tgcrypto` compiles from the sdist (verified) and is now a **required** capability (D27)
- [x] `config/default.toml`, `config/local.toml.example`, `.env.example`; `git check-ignore` confirms both
      `.env` and `data/`
- [x] `app/config.py` — 4-layer chain, `extra="forbid"` on TOML, `SecretStr`, `SecretsProvider`
- [x] `app/logging.py` (secret redaction), `app/models.py`, Alembic baseline (nine tables, applied).
      Alembic autogenerate emits single-quoted, 79-column code, so **any regenerated or new migration
      needs `ruff format migrations/` afterwards** — otherwise `ruff format --check` fails and the next
      `ruff format .` re-churns the file
- [x] `data/sessions/` and `data/cookies/` created service-account-only + startup world-readable warning
- [ ] Message catalog seeded from spec §7 — §7.1/§7.2 (onboarding, help, usage) live in `app/catalog.py`;
      §7.3–§7.8 land with the features that print them
- [x] `main.py` + `handlers/start.py` + `handlers/help.py` via smart plugins + admin guard
- [x] `app/capabilities.py` + `spideybot check`, so a missing `tgcrypto`/`ffmpeg` is never silent (D27)
- [x] First `tests/unit`: tier limits and precedence, catalog rendering (asserting no placeholder
      survives), redaction including the 3.14 mapping-argument path, filesystem layout, Kurigram wiring,
      both handlers driven directly, capabilities — 109 tests

**Done when:** the bot starts, migrates a fresh Postgres, answers `/start` and `/help`, `pytest tests/unit` green.
— **Met** (batch 1), with one caveat stated rather than hidden: Telegram routing a real user's message into
the handler was not exercised, because that needs a human to message the bot. Everything up to delivery is
covered by tests, and the live startup proved both handlers load.

### P1 — Identity, tiers, quota gates
- [x] Onboarding state machine: registration, silent referral capture, first-vs-repeat `/start` (FR-1)
- [x] Tier resolution with expiry revert; admin precedence (FR-2)
- [x] Quota check / reserve / commit / release; daily boundary (FR-3.2, FR-15, §8.5). The reservation
      is one `INSERT … ON CONFLICT DO UPDATE … WHERE` statement, so a check and an increment cannot be
      interleaved; a ten-way concurrent test proves the allowance cannot be oversold
- [x] `/quota` with `∞` rendering (FR-15, AC-10). Reads the same rows the gates read rather than
      recomputing them, so the display cannot drift from the enforcement. `0` deliberately means
      different things for a count (`no allowance`, matching `reserve_download`) and a size
      (`unlimited`, spec 13), and both are exercised
- [x] Submission rate limiter (D22): per-user token bucket, injected clock. A fixed window would let
      a user spend the whole allowance at 11:59:59 and the next at 12:00:00, so it refills
      continuously instead. Buckets are swept out once they refill, and `check` sweeps idle ones
      itself past a threshold, so the dictionary cannot grow with every user ever seen

**Done when:** AC-1 and AC-10 pass. — **Both met**; AC-1 and AC-10 are covered in `tests/acceptance`.

### P2 — Queue, jobs, cancellation
- [x] `app/queue.py`: priority + submission order, lease claim/renew/requeue (FR-10).
      Verified against Postgres, because these are database properties: four concurrent claimers
      never share a job (`FOR UPDATE SKIP LOCKED`), the slot count travels inside the claiming
      statement so it cannot oversell a tier, a stale claim cannot renew its way back in, and a
      job that already finished cannot be terminated a second time (E26)
- [x] Workers: per-tier slots, release on terminal state. The pool holds `max_concurrent_downloads`
      tasks and the *claim* applies the per-tier limit, so concurrency policy lives in one place;
      a lease-renewal task keeps a long download from being handed to a second worker, and a run
      that lost its lease discards its result instead of overwriting the new owner's. Cancellation
      reaches a running job through the keeper, which is how FR-11.6 is achievable at all
- [x] `/cancel` and `/status` with ownership/unknown-id refusals (FR-11, FR-12). The command only
      *asks*: whether a job stops at once (it was still waiting) or a request is recorded for the
      worker to act on (it was mid-transfer) is decided in `app/queue` with the row in hand, so the
      refusals cannot be raced. A requested cancellation already reads as `❌ Cancelled`, since
      showing `🔄 Running` while the worker unwinds would look like `/cancel` did nothing
- [x] Restart recovery gated on `files_sent`; delivered jobs never re-run (§8.2, E26). The
      `job_items` ledger itself arrives with the pipeline (P3), but the recovery half is verified:
      a job abandoned by a killed process is resumed and delivered exactly once, a delivered job
      is never picked up again by the new process, and a cancellation requested before the crash
      is honoured rather than revived
- [x] Scheduler: lease sweep, retention, orphan sweep (D18, D21). **Not yet started by `run`**: a
      worker with no job handler would claim jobs and fail them, so the pool and the scheduler are
      wired up with the pipeline in P3. The claim/lease/recovery path itself is verified here

      Deliberately **no daily reset job**: usage is one row per user per day, so a new day *is* an
      empty allowance. A scheduled reset would be a second implementation of the same rule, and the
      wrong one whenever the process was down at midnight; `tests/integration/test_scheduler.py`
      tests the boundary instead. The injected clock (D18) is what makes "retention is due"
      testable without waiting an hour

**Done when:** AC-4, AC-5, AC-6 pass, including kill-and-restart with no re-delivery.

### P3 — Download pipeline & delivery (engine-first, D6)

**Probed 2026-10 with gallery-dl 1.32.15 / yt-dlp 2026.08.19 — the engine output is not what the
plan assumed, so these are requirements rather than notes (details in R13):**

- `gallery-dl -j --simulate <url>` prints **one pretty-printed JSON array**, not one object per
  line. The whole stdout is a single `json.loads`, and it is ~47 KB for a one-file page.
- Every record is **typed and positional**: `[2, {metadata}]` is a directory/header,
  `[3, "<url>", {metadata}]` carries the file URL in slot 1, and `[-1, {error, message}]` is a
  failure. There is no `url` key to read — the URL is not a dict field.
- **`gallery-dl` exits 0 even when it resolved nothing**: a 404 came back as exit 0 plus a `[-1]`
  record. Exit code alone is not a success signal, so the fetcher must scan for negative codes.
- **`gallery-dl` does not handle YouTube by default** ("Unsupported URL", exit 64). The `ytdl`
  extractor must be enabled with `-o extractor.ytdl.enabled=true`, and then the `[3]` record's URL
  slot is a **`ytdl:` marker with `extension: None`** — the real, time-limited media URLs live in
  `metadata["formats"]` (53 entries here) and `metadata["requested_formats"]` (2). Because those
  URLs carry `expire=…`, resolution and transfer must stay in the same job: resolving a link and
  fetching it an hour later would use a dead URL.
- `yt-dlp -j` prints **one JSON object** (single document, 79 keys here) — a different shape from
  gallery-dl, so the two engines need separate parsers rather than one shared "JSON lines" reader.
- `yt-dlp`'s top-level dict can have **no `url` at all**: this video resolved to a merge
  (`protocol: https+https`, `requested_formats` of length 2), with the URLs only inside
  `requested_formats`. So D25's "fetch the resolved URL ourselves" needs an explicit rule for the
  merge case — either two fetches plus an ffmpeg merge, or a delegated download — and must never
  read `webpage_url` as if it were media (R13's warning, now confirmed by observation).
- **There are no single-file formats left on YouTube** (0 of 53 here, 0 of 24 on a second video),
  so *every* YouTube job is a two-stream merge and ffmpeg is on the critical path for all of them,
  not only for high-quality picks (D27).
- Resolved URLs may carry tracking parameters (`?utm_source=…`); they are kept verbatim, since
  stripping query strings would break signed URLs.
- Per-site behaviour (which hosts need a native adapter, what each one needs to resolve) is
  grounded in the previous implementation: `downloader/site/*.py` and `downloader/flow.py` on
  branch `V5`, which is also where the native → gallery-dl → cyberdrop-dl ordering comes from.

- [x] `downloader/selection.py`: which streams to fetch, and whether the result will play (D28).
      Written against the *real* format lists, so the traps it closes were observed rather than
      imagined: `formats` contains storyboard entries (`protocol: mhtml`) a naive largest-pick can
      select, and a stream with no URL is not a candidate at all — which is what stops a
      `ytdl:`-marked item being "fetched" as though its marker were media (R13)
- [x] Native YouTube adapter: resolve with `yt_dlp.YoutubeDL(...).extract_info(url, download=False)`,
      choose with `downloader/selection.py`, fetch and merge ourselves. This is a *native* adapter under
      D6's rule because the engine fails D25 here, not because YouTube needs special handling —
      gallery-dl's `ytdl` route yields a marker rather than media. Carried over from `V5`'s
      `_fetch_ytdlp_fallback`, which proved the library call. **Not** carried over: V5's primary path,
      a POST to `api.vidssave.com` with a hardcoded token (`auth: 20250901majwlqo`) and a
      `Referer` from someone else's site — an expiring third-party dependency that would sit in the
      middle of every YouTube download, for a resolve we can do ourselves. Implemented as
      `downloader/sites/youtube.py` over `probe.resolve_ytdlp` (the library, not the CLI), and seen
      resolving a real link in §5's matrix
- [x] `downloader/contracts.py` + `downloader/probe.py`: the resolver contract and the engines' output
      (D30). Two engines, two shapes, one contract: gallery-dl's typed records (`[3, url, meta]`,
      `[-1, error]`) and yt-dlp's single info dict are both translated into files and transfers.
      Guards that exist because the probes contradicted the plan: the `-o extractor.ytdl.enabled=true`
      flag is asserted present (without it YouTube is "Unsupported URL", exit 64), a `ytdl:` marker is
      **never** fetched as media but re-resolved from the metadata's formats, and no code path reads
      the exit code as a success signal. Both guards were mutation-checked — disabling the marker rule
      produces a file whose only transfer is the marker itself, which is R13's trap exactly
- [x] `downloader/fetcher.py`: stream each transfer to disk, merge two streams with `-c copy`,
      report progress, and enforce D31's cap. Verified against a real HTTP server on loopback and
      real ffmpeg output (the merge is confirmed by `ffprobe` finding both streams), because the
      behaviour under test *is* the transport. Three findings from doing it rather than reasoning
      about it: `aiter_content(chunk_size=…)` is ignored by curl_cffi, so the reporting interval is
      ours and the read size is libcurl's; a body that ends short raises `IncompleteRead`, so a
      partial cannot masquerade as a whole file; and a caller-owned session survives the fetch,
      which is what lets one worker see one connection pool instead of one per file. The cap is the
      *file's* cap — the second stream is charged the first stream's bytes, or a merge could take
      twice the limit by arriving in two pieces (mutation-checked)
- [x] `downloader/flow.py`: the pipeline for one link — the chain (native → gallery-dl →
      cyberdrop-dl last, D6), AC-3's refusal decided before any transfer, the per-file loop with
      FR-9's failure collection, and the outcome vocabulary the user's messages are built from.
      Written with every external effect injected — the chain, the stop signal, three notification
      hooks, the HTTP session — so the whole thing is driven in tests with no network, no database
      and no Telegram. Decisions this forced are recorded as D34–D37: E4 and E2 are distinguished by
      a machine-readable `empty` flag rather than by the reason's wording; bandwidth is charged for
      what the network actually moved, partials included; FR-11.6's cancellation is polled between
      chunks so a cancel is not ignored until the current file ends; and same-titled files in one
      share are numbered instead of overwriting each other. Mutation-checked on six properties (skip
      the gate, stop the loop on a failure, drop D31's running cap, report an empty share as a
      failure, overwrite the duplicate name, ignore cancellation) — every one fails a test
- [x] `downloader/registry.py`: host → chain, and the delegate. The chain is native adapter (if any) →
      yt-dlp for the video hosts it handles natively → gallery-dl, with cyberdrop-dl supplied as a
      *delegating* last resort (D38/D39). Asserted by *name*, because the names are what E2's notice
      shows the user. Two deliberate absences: no copy of the tool's host list (it knows its own, and
      a copy only goes stale), and no cookie lookup (D8: the caller resolves the file, the registry
      passes it)
- [x] Engines: gallery-dl and yt-dlp are wired through `probe.resolve_gallerydl`/`resolve_ytdlp`, and
      `--cookies` reaches both. cyberdrop-dl is wired as the delegate. An extractor *credential*
      reaches gallery-dl as `-o` (D45), which supersedes D26's committed config file — the file was
      never created, because an option cannot be shadowed by the operator's own configuration.
      Per-engine forcing arrived with D44: the chain is composed from a table, so which engine a host
      gets is a row rather than the router's default
- [x] The delegated last resort (`downloader/cyberdropdl.py`, D38): a per-run generated config whose
      keys are checked against the tool's own defaults in a test, `--dump-json` for machine-readable
      results, its CSVs read for the failure reason, logs pointed outside the download directory, and
      the cap enforced by watching the bytes on disk and killing the run — which is a *real* kill of
      the child process, not a cancelled task, or the download would continue after the user was told
      it stopped. Its exit code is ignored for success because it is 0 even for an unsupported URL
- [x] Native adapters: TeraBox (account pool + per-user cookie), Doodstream, StreamTape, MixDrop,
      StreamWish, Vidmonstr, Vidara, Luluvdoo, Bysejikuar, MEGA, YouTube, and — added 2026-10 at the
      operator's direction, after finding gallery-dl alone does not cover them — Twitter/X, Reddit,
      TikTok, Douyin, Threads, Pinterest, Tumblr, Snapchat, LinkedIn, CapCut, Dailymotion, Kuaishou,
      SoundCloud, Spotify. Each is a `Resolver` in `ChainContext.natives` via
      `registry._NATIVE_TABLE`; each has offline tests over the payload shape its site answers with
      (`tests/unit/test_sites.py`), and the third-party bridges additionally need the live smoke
      matrix below, because a payload fixture cannot prove a site still answers that way. Two of V5's
      modules are deliberately *not* here — the Cyberdrop family (D47) and Bluesky (D49) — because
      each turned out to be the engines' job, and a native adapter that always fails is a slower path
      to the same answer plus one more thing that can break
- [x] Fallback chain with a visible phase change (E2, §3): each new attempt emits E2's notice with
      the engine's own name (`catalog.fallback_notice`), which renders the spec's sentence verbatim
      for the gallery-dl step. The phases themselves are announced through `on_phase` (FR-7.3)
- [x] Pre-transfer size gate (`downloader/gate.py`) and the failure block (`app/catalog.py`), both
      pure so they can be verified before any engine call exists. AC-3's refusal is decided on what
      resolution reported, never after transfer; the failure list is capped at 10 bullets with a
      remainder (FR-9.3/9.4) and a filename with brackets is escaped so it cannot break the link
      markup it appears in. An unreported size passes but is flagged for enforcement during transfer
      (D31) — checked because `0` and `None` had already been conflated once in D30's code
- [x] Empty share (E4); 0-byte → `empty file (0 bytes)` (E6). **Both are done**: E4 is a
      `LinkResult` with `empty_share` set, distinct from E2's failure path (D34), and a zero-byte
      body is discarded and reported with FR-9.5's exact reason
- [x] Album delivery ≤10, captions, reply-to-command, never deliver twice (FR-8). `telegram/delivery.py`
      sends by kind, in albums of at most `ux.album_size`, replying to the job's message, with the
      source link as the caption (FR-8.3 leaves the wording to us). "Never twice" is the ledger's job
      (D41): a file already recorded as uploaded is skipped, and a delivery that fails is recorded as
      a failure rather than stopping the rest (FR-9.1)
- [x] Failure block `⚠️ N file(s) failed:` with ≤10 bullets + remainder (FR-9, E27). Asserted end to
      end: a job with three good files and two 404s delivers the three, names both failures with
      their reason and their link, and reports `Sent 3/5 files` — the message is the done line plus
      the block (FR-7.9 + FR-9.4), not the block alone
- [x] The job handler (`app/jobs.py`, D40): the worker pool now has something to run. It reads the
      tier's limit, builds the chain and the delegate, runs the pipeline with the lease's stop flag,
      ledgers what was delivered *before* sending the next file, charges `bytes_in` for what the
      network moved, releases the day's download when nothing was delivered, and ends the job with
      one message. `JobOutcome.retry` is false for a refusal or an empty share (D42), because those
      verdicts cannot change and retrying them would spend three attempts on the same answer

**Done when:** AC-2, AC-3, AC-15 pass.

### P4 — Live status UX
- [x] `telegram/status.py`: header / rows / footer, phases `Resolving → Downloading → Uploading → Done`.
      One renderer per job, editing the message `/dl` already posted — the job's stored
      `status_message_id` is passed in, which is what makes the "one message per job" rule (FR-7.1)
      hold across a worker restart rather than only within one attempt
- [x] Animated ellipsis while a transfer is active (FR-7.6); counters for files and bytes, and
      FR-7.5's per-file upload context (`Uploading (file 7/25)`) reported from `delivery.deliver`
      through an `on_upload` hook
- [x] Coalescing at `render_interval` / `anim_interval` with an **injected clock**, so the intervals
      are tested without sleeping; the final result bypasses both (FR-7.7, E8)
- [x] Deleted-status recreation, once (E7); `FloodWait` waited out and retried, then dropped rather
      than raised — a report must never fail the download it reports on; replace-not-append on
      completion (FR-7.9)
- [x] Failure count in the rows (FR-7.4), taken from the pipeline's returned failures because that is
      where per-file failures are collected

**Done when:** AC-2 asserts phase progression on one message id; E7/E8 replays pass. **Met:**
`tests/unit/test_status.py` drives the phases, both intervals, E7 and E8 against a scripted client and
a hand-advanced clock; `test_ac2_*` asserts the delivered status and the replaced message end to end.

### P5 — Telegram downloads & account sessions
- [x] `/login` phone → code → 2FA with the spec §7.6 error copy (E13/E14/E15). `telegram/login.py` is
      the state machine (Pyrogram has no conversation handler, so the state is explicit) and
      `handlers/login.py` is the routing: `/login` starts it, and a plain text message *while a login
      is open* continues it. Every step's outcome is a sentence rather than an exception, because
      every step ends by telling the user something. Amended from the spec's copy in one place: §7.6's
      warning says the session is stored "encrypted", and nothing is (D9), so the sentence says what
      actually happens
- [x] `/logout` = `log_out()` **then** delete the file (D10); `/account` badges from file presence.
      `handlers/account.py`. The order is the rule and the failure is reported: if Telegram cannot be
      told, the local file still goes and the user is told the session may still be listed in their
      devices
- [x] Kurigram File Storage at `data/sessions/<user_id>.session`; reconnect on `/start` (§8.3).
      `telegram/clients.build_user` composes `workdir`/`name` the way the library does, so the file
      the client writes is the file the badge looks for; `/start` reconnects through the pool and
      reports what it found — connected, saved-but-unreachable, or gone
- [x] Client identity profile (D16); live cap + idle reaping + LRU. `telegram/sessions.py`: the cap
      evicts the least-recently-used *idle* client and refuses when every live client is busy (R3),
      the idle timeout is enforced by `scheduler.tick` alongside the lease sweep, and every reconnect
      is *proved* with `get_me()` — a revoked key connects and then refuses to answer, so a connect
      that returns is not evidence (the same rule D46 applies to Reddit)
- [x] `/dt` for message / channel / folder / **range** links, captions preserved, counts reported
      (FR-4). `telegram/links.py` parses (four forms, plus topic links and query tails, with FR-4.6's
      specific refusals), `telegram/media.py` fetches with the *user's* session, and the job runs
      through the ordinary queue — so `/dt` and `/dl` share one admission policy, one ledger and one
      status message. Captions are carried on the file (`FetchedFile.caption`, FR-4.4) and the counts
      ride in `LinkResult.summary` (FR-4.5), which is how a source's own sentence reaches the final
      message without `app.messages` learning about Telegram

**Done when:** AC-7 passes and a restart reconnects from the session file. **Met:**
`tests/acceptance/test_ac7_telegram.py` drives all three of AC-7's bullets (refusal without a
session; a message link delivered with its caption; a range reporting files *and* messages scanned),
plus the album, one-file-failed, oversize-refusal and dead-session cases; `tests/unit/test_login.py`
walks the conversation including E13/E14/E15 and the timeout; `tests/unit/test_sessions.py` covers the
cap, LRU, reaping and what a dead session does to its file.

### P6 — Reply triggers
- [x] Magic word: outgoing single-token match, per-user word, guidance replies, Saved Messages
      delivery (FR-5). `telegram/magic.py`; the handler is attached to the *user's* client when the
      pool connects it, because FR-5.3's "only the user's own outgoing messages" means Telegram has
      to send them to that account. The word is matched, not filtered: it can change at any time
      (FR-14), and a filter built from a changing value is a filter that gets forgotten. All three
      outcomes — saved, no reply, no media — are said in Saved Messages, the only chat the bot may
      speak in for this feature
- [x] TimerMedia: incoming private photo/video with `media_unread`, sender caption, toggle, fail-open
      (FR-6). `telegram/timer.py`; unset is *not* off (the column is nullable so a default can change
      later), an unreadable preference stays on (FR-6.4), and a per-process debounce stops a repeated
      update saving the same photo twice. See D55 for the one part of FR-6.2 the library cannot give us

**Done when:** AC-8 and AC-9 pass, including the "changed magic word disables `WOW`" case. **Met:**
`tests/acceptance/test_ac8_ac9_triggers.py` drives both features against a real database and a
recording user client: the saved case, the changed word (old ignored, new works), an incoming `WOW`
that is *not* a command, a sentence containing the word, both guidance replies, the toggle off, the
default-on rule, the group-photo exclusion, the twice-seen debounce, and both failure paths.

### P7 — Settings, sites, bypass, referrals
- [x] Settings menu shell, TimerMedia/magic-word preferences, masked TeraBox cookie status, and
      `/sites` support listing (`handlers/setting.py`, `handlers/sites.py`). The TeraBox preference
      column is represented by migration `8588974059d3`.
- [x] Referral statistics and invite-link presentation (`handlers/referral.py`).
- [x] `/setting` inline menu: cookie Set / View(masked) / Delete(confirm); TimerMedia toggle; magic word (FR-14)
- [x] `/sites` (FR-16) and `/bypass` command integration with clear validation/network failures.
      The current FR-18 adapter resolves ordinary HTTP redirects only; JavaScript/token/API/
      Cloudflare-gated families are recognized only when a dedicated adapter is added.
- [x] Referrals: invite link, `?start=` capture, pending → credited on first successful download,
      +10/day for 30 days, self/duplicate rejection (FR-17)

**Done when:** AC-11, AC-12, AC-13 pass; no full cookie value appears in UI or logs.

### P8 — Admin, hardening, acceptance
- [ ] `/stats`, `/addpremium`, `/removepremium`, `/checkpremium` + admin-only refusal (FR-19, AC-14)
- [ ] Structured logs with redaction; temp/orphan cleanup and retention (NFR-6, D21)
- [ ] Full AC-1…AC-15 suite in CI; `pytest -m live` suite (D19)
- [ ] Multi-stage Docker image (tgcrypto build stage); README with the three-provider Postgres table —
      **the image half is done and verified** (`Dockerfile` + `.dockerignore`: `tgcrypto` compiles in the
      builder, `spideybot check` reports `ok` in the container, and no `.env`/`data/` reaches a layer).
      Measured layers: ffmpeg 457 MB, pip install 331 MB, Python base 88 MB. Two cheap wins are applied —
      the 77 MB wheels layer is gone (mounted, not copied) and dev tooling no longer ships
      (`requirements-dev.txt`). Still open: a **static ffmpeg build would save a further ~380 MB**, plus
      a compose service and the README's provider table

**Done when:** every AC in spec §11 has a named automated test and `docker compose up` runs the bot.

---

## 5. Test strategy

| Layer | Scope | Infrastructure |
|---|---|---|
| `tests/unit` | tier/quota math, referral rules, catalog rendering, album batching, status coalescing, mask helpers, the fetcher, the merge, the per-link pipeline, the payload shape every native adapter parses, the `t.me` link grammar, the session pool (cap, LRU, reaping, dead sessions) and the `/login` conversation | none (the fetcher's and the pipeline's tests run a real HTTP server on loopback and generate their media with ffmpeg — see below) |
| `tests/integration` | queue leases, worker slots, restart recovery, repositories | Postgres via the layered DSN resolver |
| `tests/acceptance` | FR/AC behaviour through handlers | `tests/fakes/` Kurigram client implementing the same protocol as the real one; a real Postgres, and the loopback server for the pipeline's end-to-end tests |
| `tests/live` (`@pytest.mark.live`) | only the non-fakeable criteria | Kurigram `test_mode=True` test servers + real hosts |

- `pytest` + `pytest-asyncio`, `asyncio_mode = "auto"`; CI runs `pytest -m "not live"`.
- **Fake transport** is the pillar: handlers depend on a narrow protocol (`send_message`,
  `edit_text`, `reply_media_group`, `download_media`, …) with the Kurigram client as one implementation,
  so AC-1…AC-15 run in CI with no network and no accounts.
- **Injected deterministic clock** into quota, referral and scheduler so the daily reset and the 30-day
  bonus window are tested exactly (§8.5).
- **The bridge adapters are tested against the *payload*, not the site** (`tests/unit/test_sites.py`).
  A scripted session answers whatever a test says, so the assertions are about naming, media kind and
  transfer role — the parts delivery depends on. What this cannot prove is that the site still answers
  that way, which is exactly why the live matrix below exists as a separate, explicit step rather than
  being folded into the suite and assumed green.

### The live smoke matrix for the bridge adapters (run 2026-10; results recorded, not assumed)

Twelve of the ported hosts are reached through a third-party "saver" endpoint (Spotify among them),
and TeraBox and Reddit additionally need an account of their own. Their payload shapes are pinned by
tests; their *liveness* is not, and pretending otherwise would be the kind of green that hides a
broken chain. So a real check is one URL each, run with
`SPIDEY_TEST_MODE=true` and `@pytest.mark.live`, expecting one of two outcomes per host — a resolved
file, or a `ResolveError` the chain moves on from:

| Group | Hosts | Needs |
|---|---|---|
| Ad-gated video hosts | Doodstream, StreamTape, MixDrop, StreamWish, Vidmonstr, Vidara, Luluvdoo, Bysejikuar | a fresh, live link each (these expiry quickly) |
| Saver-endpoint platforms | Twitter/X, TikTok, Douyin, Threads, Pinterest, Tumblr, Snapchat, LinkedIn, CapCut, Dailymotion, SoundCloud | nothing but a public post URL |
| Account-backed | TeraBox, Reddit, Spotify | a TeraBox cookie file, Reddit credentials, a public track |
| Engines, and the two hosts with no adapter | YouTube, MEGA, and the Cyberdrop family (D47) | nothing, plus the cyberdrop-dl delegate installed |

The matrix has now been **run once**, and its result is the reason four of the changes above exist.
What it measured, by host:

| Outcome | Hosts | What was measured |
|---|---|---|
| Resolves a real link | YouTube (yt-dlp), Reddit (PRAW, D46), Kuaishou (both link forms, D48), Dailymotion (ssdown.app) | Reddit's video is the strongest single evidence: it resolves to a 720p CMAF playlist **plus** a separate audio file and downloads to a playable 1.25 MB result, which is what found D43's audio-dropping bug |
| Refuses correctly, and the chain moves on | Twitter/X on a **media-less** post (`twitter.com/jack/status/20`) | vxtwitter answers "no media", which is E4's `empty` verdict rather than a failure — and the fallback's later generic error used to overwrite it, so the fix preserves the precise verdict when both attempts fail |
| Host is dead, adapter removed | Cyberdrop (crash, D47), Bluesky (bridge serves no real post, D49), Kuaishou's original bridge (NXDOMAIN, R17) | the matrix's most valuable outputs: three adapters that could never have worked |
| Inconclusive — synthetic ids only | MEGA, TikTok, Douyin, Threads, Pinterest, Tumblr, Snapchat, LinkedIn, CapCut | the probe's fabricated ids are not real posts, so a refusal proves nothing about the host. These rows are **owed a real link**, and until then they are untested rather than working |
| Locally DNS-blocked (public resolvers answer) | TikTok (`ssstik.io`), SoundCloud (`urlmp4.com`) | the proxy this checkout sits behind blocks local resolution while public DNS resolves, so the row was skipped rather than failed — `probe` failing is not the host being down (R17) |
| Reachable but unverified end-to-end | Spotify (musicfab.io), the eight ad-gated hosts | the bridge answered but offered no track for the public id used, and the ad-gated hosts need a fresh link apiece by nature (they expire within hours) |

The honest reading of that table is that **the matrix is half a pass**: the hosts that matter most
(cinema-grade engines, Reddit, YouTube, the ad-gated family's *parsing*) are covered by tests, the
two removals are settled facts, and nine adapters are still waiting for one real link each. That is
recorded here rather than smoothed over, and the next live run owes those nine rows.

- A third kind of test joins those two: **anything whose behaviour *is* the transport is tested against
  the real thing, not a stub.** `tests/fakes/server.py` is a `ThreadingHTTPServer` on loopback that
  serves bodies a test writes (and generated ones it asks for), records the headers it receives, and
  can truncate a body or stall mid-response on request. The fetcher's and the pipeline's tests both
  drive it, and generate their media fixtures with ffmpeg/ffprobe. Stubbing curl_cffi or ffmpeg would
  have asserted my beliefs instead of their behaviour — as it turned out, it would have missed that
  `chunk_size` is ignored, that a truncated body raises, and that the merge was being handed byte
  counts where it needed paths. The pipeline's *chain* is faked, because that logic is ours; the
  transport underneath it is not.
**Live suite** (`@pytest.mark.live`, skipped unless `SPIDEY_TEST_MODE=true`) runs against Telegram's
**test DCs** with throwaway accounts — never production — using `in_memory=True` clients so no session
files are left behind. Login, 2FA, session lifecycle, the magic word and TimerMedia are exercised by
running two test accounts against each other, and because test-DC content is ours to create, AC-7
(message, channel and range links with caption preservation) is fully automatable there. Only genuinely
real-world inputs still need production: a real public channel, a live TeraBox link, live
Cloudflare-gated shorteners (AC-13) and a real multi-file share (AC-15), plus AC-2/AC-3 at scale.
Targets come from config/env, so no credentials are ever committed.
- Naming: `tests/acceptance/test_ac_01_onboarding.py` … `test_ac_15_failure_transparency.py`.

---

## 6. Configuration & secrets

Layering, lowest to highest precedence:

```
code defaults  →  config/default.toml  →  config/local.toml  →  environment / .env
```

Secrets never appear in TOML; they are read through `SecretsProvider` (env today; keyring or Docker
secrets later, additively).

`config/default.toml` — committed, the §13 contract as reviewable data. `0` in a size field means unlimited:

```toml
[tier.free]
link_total_mb = 500
daily_downloads = 10
daily_mb = 1024
concurrent = 1
priority = 2.0

[tier.pro]
link_total_mb = 10240
daily_downloads = 50
daily_mb = 25600
concurrent = 2
priority = 1.0

[tier.premium]
link_total_mb = 0
daily_downloads = 200
daily_mb = 102400
concurrent = 3
priority = 0.5

[admin]
priority = 0.2

[client]
profile = "desktop"        # desktop | android | custom  (D16)

# Presets taken verbatim from the Kurigram docs. This identity is what a user sees in
# their own Active Sessions, so it is an account-safety setting, not cosmetics (D16).
# The docs' lang_code is a placeholder - use a real ISO 639-1 code.
[client.desktop]
device_model = "Desktop"
system_version = "Windows 11 x64"
app_version = "6.0.2 x64"
lang_pack = "tdesktop"
lang_code = "en"
platform = "DESKTOP"

[client.android]
device_model = "Samsung SM-S931B"   # device MODEL, not the marketing name
system_version = "15 (35)"
app_version = "11.13.2 (60601)"
lang_pack = "android"
lang_code = "en"
platform = "ANDROID"

[ux]
album_size = 10
failure_list_limit = 10
render_interval = 3.0
anim_interval = 1.2
default_magic_word = "WOW"
timer_media_default = true

[referral]
daily_bonus = 10
bonus_days = 30

[runtime]
max_concurrent = 20
job_lease_seconds = 300
job_max_attempts = 3
rate_limit_per_minute = 20
session_max_live = 50
session_idle_seconds = 900

[db]
host = "127.0.0.1"
port = 55432
name = "spideybot"
user = "spideybot"
# password is a secret -> SPIDEY_DB_PASSWORD

[download]
retention_days = 14
temp_root = "data/tmp"

[testing]
# Live mode itself is switched on by SPIDEY_TEST_MODE (see .env.example). It is a
# test-run concern rather than bot behaviour, so it stays out of the config file.
session_root = "data/sessions/test"
```

`config/local.toml` — optional, gitignored, machine/deployment overrides.
`.env` — gitignored, **secrets only**: `SPIDEY_TG_API_ID`, `SPIDEY_TG_API_HASH`, `SPIDEY_TG_BOT_TOKEN`,
`SPIDEY_DB_PASSWORD`, `SPIDEY_ADMIN_IDS`, TeraBox cookies/jstoken/bdstoken, gallery-dl + Reddit creds.
**`SPIDEY_SESSION_ENCRYPT_KEY` is removed** (D9). Live-test entries (`SPIDEY_TEST_MODE`,
`SPIDEY_TEST_PHONE`, `SPIDEY_TEST_PHONE_B`) are documented in `.env.example` but stay empty by default.
**An empty value must be treated as *unset*** — not as an empty host, port or mode — since the template
ships every key present-but-blank.
`.env.example` — committed, placeholders only.

Validation: TOML loaded with `extra="forbid"` so a typo'd key fails at startup; secrets typed as
`SecretStr`; numeric ranges validated; the log formatter redacts registered secret values (NFR-6). The
DB password is never embedded in a URL — the DSN is assembled at runtime from `[db]` plus the secret.

---

## 7. Requirement traceability

| Requirement | Phase | Primary module | Acceptance |
|---|---|---|---|
| FR-1 onboarding, referral capture, already-started | P1 | `app/` onboarding + `handlers/start.py` | AC-1 |
| FR-2 help | P0 | `handlers/help.py` | AC-1 |
| FR-3 `/dl` + quota validation | P1, P3 | `app/quota.py`, `handlers/download.py` | AC-2, AC-3, AC-10 |
| FR-4 `/dt` | P5 | `downloader/telegram_source` + `handlers/tg_download.py` | AC-7 |
| FR-5 magic word | P6 | `handlers/magic_word.py` | AC-8 |
| FR-6 TimerMedia | P6 | `handlers/timer_media.py` | AC-9 |
| FR-7 progress/status | P4 | `telegram/status.py`; phases and per-file bytes from `downloader/flow.py` | AC-2, E7, E8 |
| FR-8 delivery format | P3 | `telegram/delivery.py` + the ledger in `app/jobs.py` | AC-2 |
| FR-9 failure reporting | P3 | `downloader/flow.py` + catalog; reasons from `fetcher.py` and cyberdrop-dl's CSVs; the block is sent by `app/jobs.py` | AC-15, E27 |
| FR-14 settings menu | P7 | `handlers/setting.py`; the cookie file reaches both engines via `downloader/registry.py` | AC-12 |
| FR-10 queue & concurrency | P2 | `app/queue.py` | AC-4 |
| FR-11 cancellation | P2 | `handlers/cancel.py` | AC-5 |
| FR-12 `/status` | P2 | `handlers/job_status.py` | AC-6 |
| FR-13 accounts/sessions | P5 | `handlers/login.py`, `app/sessions.py` | AC-7 |
| FR-14 settings menu | P7 | `handlers/setting.py` | AC-12 |
| FR-15 `/quota` | P1 | `handlers/quota_info.py` | AC-10 |
| FR-16 `/sites` | P7 | `handlers/sites.py` | — |
| FR-17 referrals | P7 | `app/referral.py`, `handlers/referral.py` | AC-11 |
| FR-18 bypass | P7 | `downloader/bypass/` | AC-13 |
| FR-19 admin tools | P8 | `handlers/admin.py` | AC-14 |
| §8 state, idempotency | P2, P3 | `app/models.py`, `app/queue.py` | AC-2, E26 |
| §9 edge cases | all | distributed | E1–E28 |
| §10 non-functional | P4, P8 | `app/logging.py`, `app/paths.py` | redaction + timing tests |

---

## 8. Risks & mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | `tgcrypto` has no cp312+ wheel, so fast AES must be **compiled from the sdist** (last release 2022-11-11: plain hand-written C, not Cython) | Installs need a C toolchain, and a future CPython could break the 2022 C | **Compiled from source and required, not optional** — the source build is verified on 3.14 (MSVC `cl`/`link`, import, IGE+CTR round-trip) and the live connection confirms Kurigram selects it. The `pyaes` fallback is 291× slower (0.59 vs 171.4 MiB/s), so it is not a safety net. Mitigations: MSVC Build Tools on Windows, a `gcc` builder stage in Docker (**verified** — the image compiles it and `spideybot check` reports `ok` inside the container), a **startup check** that reports a missing accelerator, and a pin plus smoke test whenever CPython moves |
| R2 | Telegram flood limits on frequent status edits | Status errors, stalled jobs | Coalesced editor + `FloodWait` sleep/retry; the final result is always sent (FR-7.7, E8) |
| R3 | Per-user clients are expensive (socket + crypto state each) | Memory/connection exhaustion | Live cap + idle reaping + LRU (D21); sessions live on disk, clients start on demand |
| R4 | Third-party hosts drift or add anti-bot gates | Downloads break silently over time | Registry + layered fallback (D5); per-engine end-to-end tests; camoufox for gated hosts. **Amended 2026-10:** twenty-seven native adapters are now wired, of which thirteen are third-party *bridges* — the largest standing breakage surface in the project. Bounded deliberately: every bridge failure is a `ResolveError`, so the chain falls through to the engines and then the delegate, and the worst case is a rejected link rather than a failed job. The shared plumbing is one module (`sites/bridge.py`) with its own tests, so a change to the request shape is one edit rather than twenty-five. The remaining exposure is that a bridge's *payload* can change without its HTTP shape changing; the fixture tests pin the parse, and only the live matrix above can detect that |
| R5 | TeraBox account pool blocked or limited (E16) | TeraBox failures | Round-robin pool, skip-on-block, clear reason including account count |
| R6 | Multi-hour jobs across a restart | Duplicate delivery or lost work | Leased queue + `job_items` ledger; delivered jobs never re-run |
| R7 | Windows dev vs Linux prod (uvloop absent, path/case sensitivity) | "Works on my machine" | `app/paths.py`; uvloop optional and guarded (D17); CI on Linux, dev on Windows |
| R8 | **Users' accounts actioned** because the client identity looks non-official | Severe — third-party account loss | Configurable client profile mimicking a real client (D16); document the trade-off; `/logout` offers genuine revocation |
| R9 | Scope creep from spec §14 (billing, web UI) | Bloat | Explicitly excluded; tiers granted administratively only |
| R10 | **Plaintext session/cookie files** (D7–D9) | A copied file is a logged-in device, no password needed | Credential directories are created **0700** (verified in the container: `0o700` on sessions, cookies and the bot's own session, with `insecure_secret_dirs()` returning `[]`), and a startup warning still fires if one is ever loosened; one file per user; never logged or exported; gitignored; accepted deliberately by the operator |
| R11 | File-based sessions pin the bot to one host | A future multi-worker deployment cannot share sessions | Accepted for v1 (single process); revisit with shared storage or a DB-backed store if scaling out |
| R12 | Legal/ToS of user-mediated downloads | Reputational | README states users may only download content they are entitled to; no moderation policy (spec §14) |
| R13 | Downloading resolved URLs ourselves (D25) | Sites that need a `Referer`, a specific `User-Agent` or per-site cookies will return 403 when we fetch directly; and a yt-dlp-backed item can resolve to a `webpage_url` or a DASH/HLS *manifest* rather than a fetchable media URL | **Probed 2026-10 with gallery-dl 1.32.15 and yt-dlp 2026.08.19; the plan's assumption was wrong in a way that would have produced a broken parser.** `gallery-dl -j` emits one pretty-printed JSON *array* whose records are typed and positional — `[3, "<url>", {meta}]` for a file, `[2, {meta}]` for a directory, `[-1, {error, message}]` for a failure — so the URL is **not** a dict key, and it is **exit 0 on failure**, which means the exit code cannot be the success signal. For yt-dlp-backed sites the record's URL slot is a **`ytdl:` marker** carrying no media at all (`extension: None`), so the fetchable URLs must be read from `metadata["formats"]` / `metadata["requested_formats"]` — and those are time-limited, so a resolved job must be fetched promptly rather than queued against a stale URL. `yt-dlp -j` emits **one JSON object** (a different shape, needing its own parser) and its top-level dict can have **no `url`**: the probed video resolved to a merge (`protocol: https+https`, `requested_formats` length 2) with the URLs only inside `requested_formats`. Neither engine offers a single-file YouTube format any more (0 of 53), so the merge path is the normal path. Per-host header/cookie map still required |
| R14 | **`.env` quoting was read differently by `python-dotenv` and Docker's `--env-file`** | A value quoted the way `.env.example` recommends loaded correctly on the host but arrived **still quoted** in a container, so startup failed validation — a failure that never reproduces locally | **Fixed.** `load_env_file` normalises `SPIDEY_*` values in the environment (`strip_env_quotes`), so one `.env` behaves identically in both places. Verified by re-running the container with the real `.env`, which previously failed with `invalid literal for int()`. `.env.example` now documents only what `--env-file` also supports, marking `${VAR}` expansion and inline comments as host-only |
| R15 | **The bundled Postgres port can become unbindable** | `db start` fails with a message about `pg_ctl`, and the real cause is three layers down | **Happened, 2026-10:** the committed default 55432 fell inside a Windows-reserved dynamic range (`55408-55507`, WinError 10013) and every integration test failed at fixture setup. The ranges move as Windows starts services, so this can recur on any checkout. Mitigations: `dbprovider.start` now binds the port *before* launching `pg_ctl` and reports the cause and the remedy (`[db] port` in `config/local.toml`); `config/local.toml` is the documented gitignored override and is where this checkout moved to 54329; and the two tests that assert the committed defaults hide `local.toml`, so a local override cannot make them fail. Left as a risk rather than fixed by auto-selecting a port, because moving an operator's database out from under them silently would be worse than a clear error |
| R16 | **PRAW's borrowed Reddit application pairs** | Reddit rate-limits or retires a shared client id, and the fallback then fails on every Reddit link | D46: configured credentials are tried first, each identity is *proved* with a request before it is used, every failure is logged with the client id that failed, and the reason reaches the user as E2's message. The pairs travel with the image, so nothing has to be configured for Reddit to work at all. Measured live: the third pair is **already dead (401)** as of 2026-10 — which is precisely the case the fall-through exists for, and why a port that only *named* the pairs was not good enough |
| R17 | **A third-party bridge can die outright, not just drift** | Every link for that host fails, and the failure looks like a broken bot rather than a dead dependency | **Happened, 2026-10:** `kuaishouvideodownloader.net`, the endpoint V5 used for Kuaishou, is **NXDOMAIN for public resolvers** — not blocked, not moved, gone — so the port could never have worked. A bridge death is invisible to the payload tests by construction, so the mitigation is partly procedural: the live smoke matrix below is run *before* a bridge is trusted, and its results are recorded there rather than assumed. When one dies the replacement is preferred to be the site's **own** endpoint rather than another saver site (D48), because that removes the dependency instead of re-siting it. The proxy this checkout sits behind also blocked local resolution of `ssstik.io` and `urlmp4.com` while public resolvers answered — so "our probe failed" and "the host is dead" are distinguished with a public DNS query before any conclusion is drawn |

---

## 9. Milestones

| Milestone | Exit criterion | Phases |
|---|---|---|
| M0 DB provider | `spideybot db start` boots Postgres with Docker stopped; integration suite passes with and without Docker | P0-prereq |
| M1 Foundations | Fresh Postgres migrates; `/start` + `/help` answer; unit suite green | P0 |
| M2 Quota | AC-1, AC-10 pass | P1 |
| M3 Queue | Restart test proves no re-delivery; AC-4/5/6 pass | P2 |
| M4 Pipeline | AC-2, AC-3, AC-15 pass | P3 |
| M5 Status UX | Single-message lifecycle + coalescing verified; E7/E8 replays pass | P4 |
| M6 Sessions | AC-7 passes; restart reconnects from the session file | P5 |
| M7 Triggers | AC-8, AC-9 pass | P6 |
| M8 Extras | AC-11, AC-12, AC-13 pass | P7 |
| M9 Release | AC-14 + full AC-1…AC-15 in CI; live suite green; compose run green | P8 |

**Sequencing:** P0-prereq → P0 → P1 → P2 are strictly ordered. P3 and P4 interleave (a pipeline with no
status view has no observable behaviour). P5 needs P2+P3; P6 needs P5; P7 and P8 need P1+P3.

---

## 10. Immediate next step

**Batches 1 and 2 are complete and committed; batch 3 is part-way through**, in the order P3 needs
rather than the order the plan lists:

| Part | State |
|---|---|
| 3a — media selection (D28) | ✅ committed |
| 3b — resolver contract + engines' output (D30, R13) | ✅ committed |
| 3c — pre-transfer size gate + failure block (D31) | ✅ committed |
| 3d — **the fetcher** (D32, D33) | ✅ committed; `downloader/fetcher.py` verified against a loopback HTTP server and real ffmpeg |
| 3e — **`downloader/flow.py`** (D34–D37) | ✅ committed; the chain, the gate, the download loop and the outcome vocabulary, all driven in tests with fake resolvers and a real server |
| 3f — **`downloader/registry.py` + the delegated last resort** (D38, D39) | ✅ committed; the chain per host, cookie plumbing to both engines, and cyberdrop-dl as a watched delegating fetch |
| 3g — **the job handler + delivery** (D40–D42) | ✅ committed; `app/jobs.py` joins pipeline → ledger → delivery → message, `telegram/delivery.py` sends by kind in albums of ten, and AC-2/AC-3/AC-15 are verified end to end against a recording client and a real database |
| 3h — `/dl` submission + live verification | ✅ committed; `handlers/download.py` carries usage, the quota reserve, the enqueue and "Queuing download…" (AC-10's second half), and the live pass below was run against real links rather than assumed |
| 3i — native adapters | ✅ committed; 25 wired through `registry._NATIVE_TABLE` — all of V5's modules except `cyberdrop` (D47) and `bluesky` (D49), plus TeraBox, which V5 does not have. Each is a `Resolver` in `ChainContext.natives` with payload tests; the live matrix in §5 records which of them have been *seen* resolving and which are still owed a real link |
| 5a — the session pool, `/login`, `/logout`, `/account`, `/start` reconnect (D50) | ✅ committed; `telegram/sessions.py` + `telegram/login.py` + `handlers/account.py`/`login.py`, with the identity profile (D16), the cap/LRU and the idle reaping the scheduler now runs every pass |
| 5b — `/dt` and the Telegram job (D51–D53) | ✅ committed; `telegram/links.py`, `telegram/media.py`, `handlers/telegram_dl.py`, the branch in `app/jobs.py`, and AC-7 verified end to end against a real database and a recording client |
| 6a — the magic word (FR-5, AC-8) | ✅ committed; `telegram/magic.py`, attached to the user's client by the pool, with every outcome said in Saved Messages |
| 6b — TimerMedia (FR-6, AC-9) | ✅ committed; `telegram/timer.py` plus `app/prefs.py` (unset ≠ off, and reading fails open), with the `media_unread` gap recorded as D55 |

**The runtime is now wired, which retires the deferral this section used to carry.** `app/runtime.py`
starts the client, installs the session pool, starts the worker pool and the scheduler, and sweeps
orphaned staging directories once at startup; `app/bot.py` stays the synchronous edge (uvloop before
any loop exists, then `asyncio.run`). The order is the contract and is documented there: the client
before the workers (so a handler's first reply cannot race registration), the pool before the workers
(so a claimed Telegram job finds it), the scheduler last, and shutdown in the mirror order.

Still outstanding from earlier batches and tracked where it lives: the Docker-free DSN resolver's second
provider (P0-prereq), the remaining §7 catalog copy (P0), and a CI job proving the `tgcrypto` source build
stays green on each CPython bump (R1).

Nothing in the pipeline stays unwired any more: the pool, the scheduler and the reaper are started by
`app/runtime.py` (P5's wiring), and `app/ratelimit.py` is started where it is needed rather than at boot.
The worker handler joins `flow.download_link` to the queue: it owns the job's staging directory, checks
`keeper.stop_requested` for FR-11.6, records `bytes_in` (D35) and `files_sent`, and keeps jobs already
delivered from running again (§8.2).

Still outstanding from earlier batches and tracked where it lives: the Docker-free DSN resolver's second
provider (P0-prereq), the remaining §7 catalog copy (P0), and a CI job proving the `tgcrypto` source build
stays green on each CPython bump (R1).

---

## 11. Spec realignment (applied)
Every statement in [FUNCTIONAL_SPEC.md](./FUNCTIONAL_SPEC.md) that promised encryption has been
rewritten, because the system no longer encrypts anything (D9):

| Where | Now says |
|---|---|
| §1.3 privacy principle | cookies and sessions are "stored in service-account-only files, masked in the UI, and never logged in the clear" |
| FR-13, last line | "stored in a local session file and are revocable via `/logout`" |
| FR-14 rule | "Cookie status page states that the cookie is stored in a local file and never logged" |
| §7.6 login message | "⚠️ Your session is stored as a local file session." |
| §7.6 cookie page | "Your cookie is stored in a local file and never logged." |
| §8.4 cookie lifecycle | "No cookie → Set (from /setting) → Stored → View (masked) → Delete" |
| NFR-4 | "Cookies and sessions are stored in files restricted to the service account; masked in the UI; never logged in the clear." |
| §12 | points at §1 of this plan for the answers |
| §13 config table | the `session.encryption_key` row is removed |

No behavioural requirement other than storage-of-credentials was altered. AC-12's masking requirement
("View shows a masked value") still holds and is unaffected — masking is a UI behaviour, not encryption.

---

## 12. Execution cadence (agreed)

Work proceeds in **batches, one per phase group**, and **stops for review after each**. No batch starts
until the previous batch's gate is met or explicitly waived, and each verified batch is committed.

| Batch | Phases | Gate before moving on | Gate requires |
|---|---|---|---|
| 1 | P0-prereq + P0 | `spideybot db start` works with Docker stopped; migrations apply; `/start` + `/help` answer; `pytest tests/unit` green | network for pip + the 51 MB binaries download |
| 2 | P1 + P2 | AC-1, AC-4, AC-5, AC-6, AC-10, plus a kill-and-restart test proving no re-delivery | a local Postgres, nothing else |
| 3a | P3 core | AC-2, AC-3, AC-15 against a real link | a working link + network |
| 3b | P3 native adapters | one smoke test per adapter | credentials for the host under test |
| 4 | P4 + P5 + P6 | AC-7 via the live suite, AC-8, AC-9 | bot token, plus a test-DC or real account |
| 5 | P7 | AC-11, AC-12, AC-13 | live shortener hosts |
| 6 | P8 | AC-14, the full suite, and a compose run | Docker, or the bundled binaries |

Rules:

- **Stop and report after each batch** — state plainly what passed and what did not; never claim a gate
  passed when it was only partially exercised.
- **Commit each verified batch**, so any batch can be reviewed or reverted in isolation. `.gitignore` must
  exist first (it does).
- **Heavy optional dependencies are deferred.** `camoufox` and `playwright` pull ~100 MB+ of browser
  binaries that only `/bypass` needs, so they are installed at batch 5, keeping batches 1–4 fast.
- **Anything not verifiable locally** — a live TeraBox download, Cloudflare-gated shorteners, a real user's
  session — is implemented and tested against fakes, then handed over as a runnable checklist rather than
  reported as passing.
- **Batch 3b is the one parallel-friendly batch**: its eleven adapters are independent modules, each with a
  smoke test. Every other batch is a strict dependency chain and would serialise regardless.
