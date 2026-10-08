# SpideyBot — Functional Specification (Engine-Agnostic)

**Purpose of this document.** This is a complete, client-facing functional specification of
SpideyBot. It describes *what the bot does and how it must behave* for end users — not how it is
built. It contains no architectural or technology mandates, so it can be used as the input to
re-implement the product on a different engine, framework, language, or hosting model.

**How to use it.** Treat §1–§10 as requirements. §11 gives testable acceptance criteria.
§12 lists decisions a new implementation must make. §13 lists the tunable parameters.
Anything not stated here is free to change in the new implementation.

---

## 0. Product in one paragraph

SpideyBot is a Telegram bot that downloads media from 30+ third‑party platforms and from Telegram
itself, then delivers the files straight back into the user's chat. The user sends a link (or
replies to media with a magic word), and the bot fetches, reports progress live, and returns the
files. Access is tiered (Free / Pro / Premium), with per‑user quotas, a persistent queue, optional
per‑user Telegram account sessions, optional per‑user TeraBox cookies, referrals, and admin tools.

---

## 1. Product overview

### 1.1 The problem it solves
Users receive download links (video sites, file hosts, cloud shares, Telegram channels). Getting
the underlying file normally requires visiting ad‑heavy sites, solving link shorteners, or manual
re‑uploading. SpideyBot collapses that into one chat command and returns the file to the user.

### 1.2 The core loop
1. User sends a link to the bot.
2. Bot validates access (quota, size, concurrency) and confirms.
3. Bot shows **one live status message** that advances through phases with per‑file progress.
4. Bot delivers the files (grouped into albums of up to 10).
5. Bot replaces the status message with the final result, including any per‑file failures.

### 1.3 Design principles (user-visible)
- **One command, files back in chat.** No website, no install beyond Telegram.
- **Exactly once.** A download request must be processed once and delivered once — never
  duplicated, never silently dropped.
- **No chat spam.** One live status message per job, edited in place rather than a stream of messages.
- **Failures are named, not hidden.** Every file that fails is listed by name with a reason; one bad
  file never aborts the batch.
- **Recover by default.** Unknown links, dead hosts, expired accounts, and transient errors are
  retried through alternate paths before the bot reports failure.
- **Privacy by default.** User cookies and account sessions are encrypted, masked in the UI, and
  never logged in the clear.

### 1.4 Actors
| Actor | Description |
|---|---|
| User | Any Telegram user, in a private chat with the bot |
| Free user | Default tier |
| Pro user | Paid/upgraded tier |
| Premium user | Highest paid tier |
| Admin | Operator with elevated privileges; always treated as top tier |

### 1.5 Channels
- Primary: **private chat** between a user and the bot.
- Supported clients: any Telegram client (mobile, desktop, web).

---

## 2. Roles & tiers

### 2.1 Tier definitions
| Tier | Downloads/day | Max total size per link | Daily bandwidth | Concurrent downloads | Queue priority |
|---|---|---|---|---|---|
| Free | 10 | 500 MB | 1 GB | 1 | Low |
| Pro | 50 | 10 GB | 25 GB | 2 | Medium |
| Premium | 200 | Unlimited | 100 GB | 3 | High |
| Admin | Unlimited | Unlimited | Unlimited | 3 (queues when full) | Highest |

- Limits are **per user, per day**, reset on a daily boundary.
- Admin is a **privilege flag**, not a purchasable tier: admins run at the top of the queue and are
  never rejected for limits — extra requests always wait in the queue.
- **Link size limit** applies to the *combined* size of every file behind a single link.

### 2.2 Elevation and expiry
- Pro/Premium grants may carry an **expiry date**. An expired grant silently reverts the user to Free.
- Tier labels shown in the UI:
  - `👑 Admin`
  - `✨ Pro`
  - `✨ Premium`
  - `👤 Free`

---

## 3. Supported content sources

The bot must download from, at minimum, the following. New implementations must preserve the
user-facing set (or exceed it).

**Social & video:** YouTube, Twitter/X, Instagram, TikTok, Reddit, Pinterest, Snapchat, LinkedIn,
Bluesky, Threads, Tumblr, SoundCloud, Spotify, Dailymotion, Douyin, Kuaishou, CapCut, Facebook.

**Video/file hosting:** Doodstream, StreamTape, MixDrop, StreamWish, Vidmonstr, Vidara,
Luluvdoo, Bysejikuar, MEGA, and Cyberdrop-family hosts (Cyberdrop, Bunkr, Cyberfile, GoFile,
Saint, IGBsa and similar).

**Heavy / cloud share:** TeraBox (single files, folders, multi-file shares).

**Telegram (native):** individual messages, channels, folders, and message-range links.

**Fallback rule (user-visible):** if a link is not recognized, or the primary extraction method
fails, the bot must automatically retry via general-purpose and alternate extraction methods. The
user sees a phase change (e.g. "Primary download failed, trying alternate method") rather than a
hard error, unless all methods fail.

---

## 4. Command surface (user-facing)

| Command | Arguments | Summary |
|---|---|---|
| `/start` | — | Register / refresh status / auto-resume saved session |
| `/help` | — | Help guide + tier, link limit, session state |
| `/dl` | `<URL>` | Download from a supported platform |
| `/dt` | `<t.me link>` | Download from Telegram (requires a live session) |
| `/cancel` | `<ID>` | Cancel your queued/running download |
| `/status` | — | List your active downloads and their state |
| `/account` | — | Account & session status |
| `/quota` | — | Usage today + limits |
| `/sites` | — | Supported platforms |
| `/referral` | — | Invite link + referral stats |
| `/login` | — | Connect a Telegram account session |
| `/logout` | — | Disconnect the session |
| `/setting` | — | Per-user preferences menu (inline buttons) |
| `/bypass` | `<URL>` | Resolve a link shortener to its destination |

**Admin-only commands**

| Command | Summary |
|---|---|
| `/stats` | Live download statistics |
| `/addpremium` | Grant Pro to a user |
| `/removepremium` | Revoke Pro from a user |
| `/checkpremium` | Check a user's premium status |

**Command parsing rules**
- Commands are case-sensitive slash commands; leading whitespace is tolerated.
- A command must match as a whole word (`/dl` must not fire on `/dload`).
- Bot `@username` suffix forms (`/dl@SpideyBot ...`) must be accepted.
- Missing/blank argument → reply with that command's usage text.
- `/start` may carry a numeric referral payload (`/start <referrer_id>`); it is optional and must
  never be treated as a usage error (FR-1, FR-17).

---

## 5. Feature requirements

### FR-1 — Onboarding (`/start`)

`/start` has three responsibilities: **registration**, **referral capture**, and **session
reconnect** — followed by the appropriate onboarding message.

1. **Register the user if new** (store ID, username if available).
2. **Detect a referral payload.** `/start` may carry an argument in the form `/start <referrer_id>`
   (produced by an invite deep link, FR-17). When the argument is a valid positive integer:
   - record a **pending** referral attributed to that referrer;
   - skip recording if it is a **self-referral**, or if the invitee **already has a referrer**
     (first-start-wins);
   - the referral is **not credited yet** — crediting happens on the invitee's first successful
     download (FR-17);
   - this step is **silent** — it must not change the onboarding message.
   An absent, non-numeric, or non-positive argument is ignored without error.
3. If the user has a saved session that is not currently connected, **reconnect it automatically**
   (so `/dl` and `/dt` work immediately afterwards).
4. **First `/start` → full welcome.** Show:
   - a personal greeting with the user's first name,
   - the tier label and the session state badge,
   - the free-tier note (free users only),
   - the essential command list,
   - an **Upgrade** button (URL button).
5. **Repeat `/start` → "already started" notice.** Once a user has completed onboarding, any
   subsequent `/start` must reply with a short *already started* confirmation instead of repeating
   the full welcome. This includes the case where the user already had a saved session: their
   **first** `/start` shows the welcome as usual, and the **next** `/start` reports that it is
   already started.
   - The notice must be brief and must not re-send the full onboarding content.
   - It should still reflect the current session state so the user knows the session is live.
6. The "already started" state is **per user** and **persists across bot restarts**.

> Note: in the current implementation, referral capture on `/start` is **not yet wired up**, and
> repeat `/start` shows the full welcome every time. Both are requirements for the new engine.

### FR-2 — Help (`/help`)
1. Show a welcome header, then the user's **tier**, **link size limit**, and **session state**.
2. Show the essential command list and the behavioral notes.
3. Admins additionally see the admin command block.
4. Help output must reflect the *caller's* limits, not global defaults.

### FR-3 — Generic link download (`/dl <URL>`)
1. Reject with usage text if no URL is supplied.
2. Validate the request against the user's quota **before** queuing:
   - daily download count,
   - daily bandwidth,
   - concurrent slots,
   - link size limit (checked once the link's contents are known).
3. On quota failure, reply with a clear, specific reason and a pointer to `/quota`.
   When the failure is concurrency, include `(used/total slots used)`.
4. On acceptance, post a status message and begin the job.
5. Report the queue position if the job must wait.

### FR-4 — Telegram download (`/dt <t.me link>`)
1. Only accept Telegram links; other URLs are rejected with a pointer to `/dl`.
2. Require a live user session. If none, tell the user to run `/start` or `/login` first.
3. Support: single message links, channel posts, folders, and **message-range links**.
4. Preserve captions on re-send. If no caption exists, generate a summary caption.
5. Report counts: files downloaded, and for ranges, messages scanned and source chat title.
6. Reject malformed Telegram links with a specific parse error.

### FR-5 — Reply "magic word" download
1. A user replies to a media message and sends their **magic word** to download it.
2. Default magic word: `WOW`, **case-insensitive**. Users can change it (FR-14).
3. Only the user's own outgoing single-token messages are considered (never other people's messages).
4. Errors:
   - Magic word sent without a reply → instruct how to use it.
   - Reply target has no media → instruct how to use it.
5. On success, deliver the media to the user's Saved Messages with a confirmation caption.

### FR-6 — TimerMedia (auto-save private media)
1. When enabled (default **ON**), automatically save photos/videos sent to the user privately.
2. Scope: incoming **private** messages from **other** users, containing **photo or video**, that
   are **unread** (`media_unread`). Ignore the user's own messages and non-media.
3. Forward the saved media to the user's Saved Messages with a caption identifying the sender:
   username (if any), sender ID, and the original text (if any).
4. Toggle via `/setting`. If the preference cannot be read, default to ON (fail open).

### FR-7 — Progress & status reporting
1. Each job owns exactly **one** status message, edited in place.
2. The status message has three zones: a **header** (current phase), optional **rows** (live
   counters/bytes), and a persistent **footer** with the cancel instruction.
3. Phases must be visible: `Resolving → Downloading → Uploading → Done`.
4. Live rows must include, where applicable:
   - files downloaded / total,
   - files uploaded / total,
   - files sent / total,
   - current transfer byte progress,
   - a failure count once any file fails.
5. **Per-file upload context** must appear (e.g. `Uploading (file 7/25)`).
6. While a transfer is active, the header shows an **animated ellipsis** so the job looks alive
   even when bytes stall.
7. Updates must be **rate-limited** (coalesced) to avoid Telegram flood limits.
8. If the status message is deleted by the user, the bot recreates it and continues reporting.
9. On completion, the status message is replaced (not appended) with the final result.

### FR-8 — Delivery format
1. Deliver files as media, preserving type where determinable (photo, video, audio, document).
2. Group independent files into **albums of up to 10**; flush the remainder at the end.
3. Each delivered file carries a caption; include the source link where useful.
4. Reply to the user's original command message for context.
5. Never deliver the same file twice within a job.

### FR-9 — Failure reporting
1. A file that fails to download or upload must **not** stop the remaining files.
2. Collect failures as `(name, reason, optional source URL)`.
3. In the final message, list up to **10** failures as bullets of the form
   `• <name> — <reason>` (name hyperlinked when a URL is known), then summarise the remainder
   (`• …and N more`).
4. The failure block is prefixed with the total: `⚠️ N file(s) failed:`.
5. Zero-byte results count as failures with reason `empty file (0 bytes)`.

### FR-10 — Queue & concurrency
1. Each tier has a fixed number of concurrent download slots (1/2/3).
2. Requests beyond the slot count **wait in a persistent queue** — they are not rejected for
   concurrency (any exception is the per-tier configuration; the shipped default queues).
3. When a slot frees, the next waiting job starts automatically.
4. Ordering is by priority, then submission order. Admin priority is highest.
5. The queue survives restarts: jobs that were not delivered resume/complete; jobs already
   delivered are never re-run.
6. `/status` must distinguish `Waiting for a slot` from `Running`.

### FR-11 — Cancellation (`/cancel <ID>`)
1. Require an ID; otherwise show usage.
2. Reject non-numeric IDs with a pointer to `/status`.
3. Reject unknown IDs ("no active download with that ID").
4. Reject IDs owned by another user.
5. On success, confirm the cancellation and, if the job owns a status message, close it with a
   "cancelled" result.
6. Cancellation must actually stop the job (download and upload phases).

### FR-12 — Status listing (`/status`)
1. With no active jobs: report `No active downloads.`
2. Otherwise list each job as `<ID> — <link (truncated)> — <state>` where state is one of:
   `🔄 Running`, `⏳ Waiting for a slot`, `❌ Cancelled`.
3. If job state cannot be read, degrade gracefully and report the job as running.

### FR-13 — Accounts & sessions
**`/login`** — multi-step, guided flow:
1. Refuse if a login is already in progress for that user.
2. Refuse if the user is already logged in, directing them to `/logout` first.
3. Prompt for phone number (accept a shared-contact button or typed `+number`).
4. Send the login code; handle invalid code, expired code, and rate-limit (report wait seconds).
5. If 2FA is enabled, prompt for the password; handle a wrong password.
6. On success: report the masked account identity; if the session started, tell the user how to
   disconnect (`/stop`) and revoke (`/logout`).
7. On failure/timeout: a clear retry message.
8. Cancellation at any step must be honored and reported.

**`/logout`**
1. Remove the user's session and confirm. If none existed, say so neutrally.

**`/account`**
1. Show the session badge and pointers to `/login` and `/logout`.
2. Badge values: `🟢 Connected`, `🟡 Session saved (not running)`, `🔴 No session`.

**Session semantics (functional):**
- Sessions persist across restarts and are reconnected on `/start`.
- A session enables Telegram downloads, the magic word, TimerMedia, and access to private/restricted
  content on the user's behalf.
- Only one session per user at a time.
- Session credentials are stored encrypted and are revocable via `/logout`.

### FR-14 — Settings menu (`/setting`)
An inline-button menu that edits the same message in place. Groups:

| Group | Actions |
|---|---|
| TeraBox Cookie | Status page → **Set** (prompt), **View** (masked), **Delete** (with confirmation) |
| TimerMedia | Status page → toggle ON/OFF |
| Magic Word | Status page → view, and **change** (prompt) |

Rules:
- Cookie is displayed **masked** (never full value); a masked `ndus` value shows first 4 + last 2 chars.
- Cookie status page states that the cookie is encrypted at rest and never logged.
- If cookie storage is unavailable, tell the user to contact an admin.
- Magic word change accepts a new single token; case-insensitive matching thereafter.
- Menu navigation returns to the main menu after each action.

### FR-15 — Quota display (`/quota`)
Show, for the caller:
1. Tier label.
2. Downloads used today (with referral bonus shown separately if any) and remaining, or `∞`.
3. Bandwidth used today against the cap, or `∞`.
4. Concurrent slots in use `active/total`, with notes when the user is admin or the tier queues.
5. Number of queued jobs waiting for a slot (only when non-zero).
6. Link size limit.
7. If the user cannot download right now, an explicit "quota reached" warning.

### FR-16 — Sites listing (`/sites`)
List supported platforms grouped into categories (social & video, video hosting, heavy/cloud), and
tell the user to send links with `/dl` or `/dt`.

### FR-17 — Referral program
1. Provide a personal invite link of the form `https://t.me/<bot_username>?start=<user_id>`.
2. **Requires the bot to have a public username**; otherwise show a clear "can't build link" notice.
3. States: **Invited** (total), **Credited**, **Pending**.
4. Credit rule: a referral is credited only when the invitee completes their **first successful
   download** — not on join.
5. Reward: **+10 downloads/day per credited invite**, valid for **30 days** from credit.
6. Rules: self-referral rejected; one referrer per invitee (first-start-wins).
7. Bonus appears as extra headroom in `/quota`.

### FR-18 — Shortener bypass (`/bypass <URL>`)
1. Resolve known link-shortener/ad-gate URLs to their final destination and return it.
2. Must handle JavaScript-gated counters and Cloudflare-protected gates.
3. Must cover at least the following families (representative hosts):
   - ouo (`ouo.press`, `ouo.io`)
   - adfly (`adfoc.us`)
   - gplinks (`gplinks.co`, `gplinks.com`)
   - cuty/cuttty, exe.io/exeygo, lksfy/linkshortify, rinku/7mb.io
   - boostellar/bstlar/bstshrt, boostylink, keyforge, linkunlocker
   - cut4money/adurl.io/shr2.link, droplink, linkvertise
   - filecrypt.cc/.to/.co, sub2unlock/sub4unlock (all domains), lootlabs/loot-link/lootdest
   - shrinkme, shrinkearn (oii.la, tpi.li), shrinkpe (aii.sh, lnbz.la)
   - 1shortlink, arolinks/vplink.in, linksterr, linkjust, liteshort, shortxlinks, genlink
   - icutlink, fc-lc.xyz/fc.lc/oii.io, bbmkts, linknext/shorte.io, tfly, mitly, linclik
   - cpm.link/cpmlink, shrtslug, ez4short/game5s/tech8s, work.ink, link4m
   - lockr.net/.so/.to, frdl.*, dlsurf/dl.surf, sfl.gl, earnlinks/linksgo.in, earn4link.in
   - ontops.link, clipi.cc, nitro-link.com, multiup.io, filepress.baby, molyn.top
   - move2link.co / siendu.com (Cloudflare + API-gated)
4. Failures must report a clear reason.

### FR-19 — Admin tools
| Command | Requirement |
|---|---|
| `/stats` | Admin only; show live counts of running and cancelled downloads |
| `/addpremium` | Admin only; grant Pro to a target user for a period (expiry shown) |
| `/removepremium` | Admin only; revoke Pro |
| `/checkpremium` | Admin only; report a user's premium status |

Admin-only commands must reject non-admins with a clear "Admin only." message.

---

## 6. Quota & tier matrix (defaults)

| Parameter | Free | Pro | Premium |
|---|---|---|---|
| Max total size per link | 500 MB | 10 GB | Unlimited |
| Daily bandwidth | 1 GB | 25 GB | 100 GB |
| Concurrent downloads | 1 | 2 | 3 |
| Queue priority | Low | Medium | High |

Derived rules:
- Admin: unlimited size and bandwidth, premium concurrency, highest priority, never rejected.
- Where a limit is unlimited, the UI displays `∞`.
- Referral bonus adds to the daily downloads allowance only.

---

## 7. Message catalog (verbatim user-facing copy)

Implementation may adjust wording, but must preserve **meaning, emoji semantics, and the
information content**. Placeholders are shown in `{braces}`.

### 7.1 Help & onboarding
```
**Welcome to SpideyBot**
Download videos, files, and media from 30+ platforms — including Terabox, YouTube, Reddit, Instagram, and more.
```
```
    Tier: {tier_label}
    Link limit: {limit_label}
    Sessions: {session_badge}
```
```
**Essential commands**

    /start  — Sign up or refresh your status
    /help   — Show this help guide
    /dl *URL*  — Download a file
    /dt *t.me link*  — Download from a Telegram message
    /cancel *ID*  — Cancel a queued download
    /status — View your active downloads
    /account — Account & session status
    /quota — View your usage & limits
    /sites — List supported platforms
    /bypass *URL* — Resolve link shorteners to final destination
    /referral — Get your invite link & stats
```
```
**Notes**

    • Your tier sets how many downloads run at once (1/2/3) — extra links wait in the queue. See /quota.
    • A finished job replaces the progress message with the result; files that
      failed to download or upload are listed there by name.
    • Terabox links require a logged-in account — use /account to manage sessions.
    • Reply to media and send your magic word (default: `WOW`) to download it.
    • Private photos/videos are auto-saved — manage via /setting.
```
```
**Admin only**

    /stats  — View live download statistics
    /addpremium — Grant Pro to a user
    /removepremium — Revoke Pro from a user
    /checkpremium — Check a user's premium status
```
```
🆓 Free tier: 1 download at a time, size limit per download.
```
```
**Welcome, {first_name}!**
```
```
ℹ️ **SpideyBot:** Already started — your session is active.
```
> The *already started* notice is shown on every `/start` after the first one. Referral capture
> (`/start <referrer_id>`) is silent and never alters either message.

### 7.2 Usage texts
```
**Usage:** /dl *URL*

Provide a link to download.
```
```
**Usage:** /dt *Telegram link*

Download directly from a Telegram message (t.me link). Requires an active session — use /start or /login.
```
```
**Usage:** /cancel *ID*

View your active downloads with /status.
```

### 7.3 Download lifecycle
```
⏳ **SpideyBot:** Starting download...
```
```
Send `/cancel {entry_id}` to abort.
```
```
🔍 **SpideyBot:** Resolving TeraBox link... (account {i}/{n})
```
```
📥 **SpideyBot:** Downloading from {site_label}
```
```
📥 **SpideyBot:** Downloading & uploading
```
```
📤 Uploading (file {index}/{total})
```
```
✅ **SpideyBot: Done!** Sent {sent}/{total} files ({size} MB).
```
```
✅ **SpideyBot: Done!** Sent {sent} file(s).
```
```
❌ **SpideyBot:** No files were successfully sent.
```
```
❌ **SpideyBot:** Download cancelled.
```
```
❌ **SpideyBot: An error occurred.**
Error: `{error}`
```
```
⚠️ **SpideyBot: Limit Exceeded.** Total share size is `{actual} MB`, exceeding your limit of `{limit}`.
```
```
ℹ️ **SpideyBot:** No actual files found in this share.
```
```
⚠️ **SpideyBot:** Primary download failed, trying gallery-dl
```

### 7.4 Queue, cancel, status
```
⏳ **SpideyBot:** Queuing download…
```
```
📋 Queued — position **#{position}**. Send `/cancel {entry_id}` to abort.
```
```
🚫 **SpideyBot:** {reason} ({active}/{concurrent} slots used)
Send /quota to check limits.
```
```
✅ No active downloads.
```
```
**Active downloads:**

  `{id}` — {link} — {state}
```
```
⚠️ **Invalid ID** — pass the number shown by /status.
```
```
⚠️ No active download with ID `{id}`.
```
```
⚠️ That download belongs to another user.
```
```
❌ Cancelled download **{id}**.
```

### 7.5 Telegram / magic word / TimerMedia
```
⚠️ **Telegram downloads need your session.**
Send /start or /login first, then use /dt again.
```
```
⚠️ **/dt is for Telegram links only.**
Use `/dl <URL>` for other sites.
```
```
⏳ **SpideyBot:** Downloading from Telegram{ (range)}...
```
```
✅ **SpideyBot:** Downloaded {downloaded} file(s) from {total} messages in `{chat_title}`.
```
```
⚠️ **Reply to a message with media and send `WOW` to download it.**
```
```
⚠️ **Replied message has no media.**
Reply to a message with media and send `WOW` to download it.
```
```
✅ **SpideyBot:** Downloaded media from replied message.
```
```
From {username}{user_id}:
"{caption}"
```
(e.g. `From @alice#id123456:` then the original text on the next line; username
and caption are omitted when absent, and the sender ID alone is used when the
sender has no public username.)

### 7.6 Account, login, logout, settings, quota, sites, referral
```
**Account Status**

    {session_badge}

To log in to Terabox, use /login.
To log out, use /logout.
```
```
🔐 **Login to your Telegram account**

This lets SpideyBot access private/restricted content on your behalf.

⚠️ Your session is stored as an encrypted file session.
You can revoke access at any time with /logout.

Please send your **phone number** (e.g. `+1234567890`):
```
```
✅ **Login successful!**

Logged in as `{masked}`.

Use `/stop` to disconnect, or `/logout` to revoke.
```
```
🔓 Logged out successfully. Your session has been removed.
```
```
ℹ️ You are not currently logged in.
```
```
⚙️ **Settings**

Choose a category to manage:
```
```
🔑 **TeraBox Cookie**

No cookie saved yet.
Set one to download from TeraBox with your own account.
```
```
🔑 **TeraBox Cookie**

• `ndus`: `{masked}`

Your cookie is encrypted at rest and never logged.
```
```
📷 **TimerMedia**

Auto-save photos/videos sent to you privately: **{ON|OFF}**

When ON, incoming media is saved to `TimerMedia/` and forwarded to you.
```
```
**Quota Status**

  Tier: {tier_label}
  Downloads today: {used}{ (+{bonus} bonus)}  — {remaining} left
  Bandwidth today: {used} / {cap}
  Concurrent: {active}/{concurrent} slots{notes}
  Queued: {n} waiting for a slot
  Link limit: {limit}

⚠️ **Quota reached** — try again tomorrow.
```
```
**Supported Platforms**

  📱 Social & video: YouTube, Twitter, TikTok, Reddit, Instagram, ...
  🎬 Video hosting: Doodstream, StreamTape, MixDrop, StreamWish, Vidmonstr, ...
  📦 Heavy / cloud: TeraBox

Send any link with /dl or /dt to download.
```
```
**Referral Program**

  🔗 Your link: {link}
  👥 Invited: {total}
  ✅ Credited: {credited}
  ⏳ Pending: {pending}

  💰 Bonus: +{bonus} downloads/day per credited invite
     (valid for {days} days after credit)
```
```
⚠️ **Can't build your invite link.**
Invite links need a public @username for the bot.
```

### 7.7 Failure block
```
⚠️ {count} file(s) failed:
• {name} — {reason}
• …and {remaining} more
```

### 7.8 Admin
```
⛔ Admin only.
```
```
**Live Stats**
  Running: {n}
  Cancelled: {n}
```

---

## 8. State model & lifecycle rules

### 8.1 Download job states
```
Queued → Running → (Delivered | Failed | Cancelled)
```
- **Queued**: accepted and validated, waiting for a concurrency slot.
- **Running**: at least one phase active (resolve/download/upload).
- **Delivered**: one or more files reached the user.
- **Failed**: no files delivered; reason reported.
- **Cancelled**: user aborted; partial files discarded.

### 8.2 Idempotency rules
1. A download request submitted via the bot **and** the user's own session must be handled **once**.
2. A job that has already delivered files must never re-run.
3. `files_sent` is tracked per job and used to gate resume/fallback paths.
4. Referral crediting is idempotent (credit once per invitee).

### 8.3 Session lifecycle
```
No session → Login (phone → code → optional 2FA) → Connected
Connected → bot restart → Session saved (not running) → /start → Connected
Connected → /logout → No session (credentials revoked)
```

### 8.4 Cookie lifecycle
```
No cookie → Set (from /setting) → Stored encrypted → View (masked) → Delete
```

### 8.5 Daily reset
- Download counts and bandwidth usage reset on a daily boundary (UTC-based).
- Referral bonuses are computed for a rolling 30-day window from each credit date.

---

## 9. Edge cases & error handling

| # | Situation | Required behavior |
|---|---|---|
| E1 | `/dl` with no URL | Show usage text |
| E2 | Unsupported link | Attempt fallback methods; if all fail, report the reason |
| E3 | Link larger than the tier limit | Abort before downloading; show actual vs allowed size |
| E4 | Share contains no files (folders only) | Inform the user; no job queued |
| E5 | Some files fail | Deliver the rest; name failures in the final message |
| E6 | A file downloads as 0 bytes | Treat as failed with reason `empty file (0 bytes)` |
| E7 | User deletes the status message mid-job | Recreate the status message and continue |
| E8 | Telegram rate-limits updates | Coalesce/rate-limit edits; never drop the final result |
| E9 | `/cancel` with wrong owner | Refuse |
| E10 | `/cancel` with unknown ID | Refuse |
| E11 | `/dt` without a session | Direct the user to `/start` or `/login` |
| E12 | `/dt` with a non-Telegram URL | Direct the user to `/dl` |
| E13 | Login code invalid/expired | Specific message; ask to retry `/login` |
| E14 | Telegram flood-wait during login | Report the wait time |
| E15 | 2FA password wrong | Specific message; ask to retry |
| E16 | Account pool exhausted / all cookies invalid | Clear failure reason; mention account count when pooled |
| E17 | Cookie storage not configured | Tell the user to contact an admin |
| E18 | TimerMedia preference unreadable | Fail open (treat as ON) |
| E19 | `/status` cannot read job state | List jobs anyway as running |
| E20 | Bot has no public username | Referral link notice instead of a broken link |
| E21 | Self-referral | Silently not recorded |
| E22 | Second referrer for same invitee | Ignored (first wins) |
| E23 | Non-admin runs an admin command | `⛔ Admin only.` |
| E24 | Concurrency full | Wait in queue; show position |
| E25 | Quota exhausted | Refuse at submission with reason + `/quota` pointer |
| E26 | Job interrupted by restart | Never re-deliver already-sent files |
| E27 | Long failure lists | Show up to 10, summarise the rest |
| E28 | Very long link in `/status` | Truncate with an ellipsis |

---

## 10. Non-functional requirements

1. **Scale & robustness**
   - Many concurrent users; per-user isolation of jobs, files, quotas, and sessions.
   - The queue persists across restarts.
   - Long jobs must not block the bot's responsiveness to other users.
2. **Throughput/latency**
   - Progress visible within ~1–3 seconds of a phase change.
   - Status edits rate-limited to stay within Telegram limits.
3. **Resource bounds**
   - Bounded memory/disk: stream and flush files in batches (≤10) rather than buffering everything;
     clean up temporary files after each job.
4. **Privacy & security**
   - Cookies and sessions encrypted at rest; masked in the UI; never logged in the clear.
   - Session revocation is immediate and user-controlled.
   - Users can only cancel/list their own jobs.
5. **Reliability**
   - Alternate extraction paths before reporting failure.
   - Account failover without user intervention.
   - Clear, actionable error copy for every failure mode.
6. **Observability (operator-facing)**
   - Admin-visible live statistics.
   - Structured logs that never contain secrets or full cookies.

---

## 11. Acceptance criteria (testable)

Each criterion should be verifiable without reference to the original implementation.

**AC-1 Onboarding**
- A brand-new user sending `/start` becomes a Free-tier user and sees the welcome, tier, commands,
  and Upgrade button.
- A returning user with a saved session sees it reconnected (`🟢 Connected`).
- A user who has already onboarded and sends `/start` again receives the short **already started**
  notice, **not** the full welcome — including a user whose saved session was just reconnected.
- `/start <referrer_id>` records a **pending** referral for that referrer, and the onboarding
  message is unchanged; `/start <own_id>` and a second `/start <other_id>` from the same invitee
  record nothing.

**AC-2 Download success**
- `/dl <supported link>` produces a status message that advances through Resolving/Downloading/
  Uploading and ends with `✅ … Done! Sent X/Y files`.
- The delivered files are replies to the command and are never duplicated.

**AC-3 Size limit**
- A link exceeding the tier limit aborts **before** transfer with actual vs allowed size and no files sent.
- The same link succeeds on Premium/Admin (unlimited).

**AC-4 Concurrency & queue**
- Free user submitting 2 links: the first runs, the second reports a queue position and starts
  automatically when the first finishes.

**AC-5 Cancellation**
- `/cancel <id>` stops the job, closes the status message as cancelled, and frees the slot.
- Another user's ID and unknown IDs are refused.

**AC-6 Status**
- `/status` lists running and queued jobs with correct states; empty state says `No active downloads.`

**AC-7 Telegram path**
- `/dt` without a session instructs the user to start one.
- With a session, a Telegram message link downloads and re-delivers the media with captions preserved.
- A range link reports files and messages counts.

**AC-8 Magic word**
- Replying `WOW` to a media message downloads it to Saved Messages.
- After changing the magic word to `go`, `go` works and `WOW` does nothing.
- Sending the magic word with no reply shows the instruction.

**AC-9 TimerMedia**
- With it ON, an incoming private photo is saved and forwarded to Saved Messages with a sender caption.
- With it OFF, nothing is saved.

**AC-10 Quota display**
- `/quota` reflects real usage and shows `∞` for unlimited dimensions.
- An exhausted user gets a refusal on the next `/dl` with a `/quota` pointer.

**AC-11 Referral**
- Referral link is shown when the bot has a username.
- An invitee is `Pending` until their first successful download, then `Credited`.
- Bonus (+10/day for 30 days) appears in the referrer's `/quota`.
- Self-referral and duplicate referrer are rejected.

**AC-12 Settings**
- Cookie Set → View shows a masked value; Delete removes it with confirmation.
- TimerMedia and Magic Word toggles/edits persist across sessions.

**AC-13 Bypass**
- A supported shortener URL resolves to the destination; unsupported ones report a clear failure.

**AC-14 Admin**
- Admins see `/stats` and premium management; non-admins are refused.
- A granted Pro user gets Pro limits and reverts to Free after expiry.

**AC-15 Failure transparency**
- When 3 of 20 files fail, the final message lists those 3 by name and reason, and 17 files arrive.

---

## 12. Open decisions for the new engine

These are deliberately unspecified; the new implementation must choose and document them.

1. **Runtime/language and bot framework.**
2. **Persistence choice** for users, quotas, queue, sessions, cookies, referrals.
3. **Media extraction toolchain** (e.g. yt-dlp, gallery-dl, cyberdrop-dl, or native resolvers) and
   the fallback order.
4. **Where per-user cookies/sessions are stored** (DB vs filesystem) and the encryption scheme.
5. **Hosting/worker model** (single process, worker pool, containerized) and how the queue is
   satisfied across processes.
6. **Localization** — whether user-facing copy becomes translatable resources.
7. **File-transfer strategy** — how Telegram uploads are streamed and how albums are grouped.
8. **Admin elevation UI** — how tiers are granted (command-only, panel, external billing).
9. **Rate-limit/backoff policy** for Telegram and third-party hosts.
10. **Cleanup policy** for temporary files and stale sessions.

---

## 13. Configurable parameters

All values below must be configurable without changing behavior contracts.

| Parameter | Default | Meaning |
|---|---|---|
| `free.link_size` | 500 MB | Max total size per link (Free) |
| `free.daily_downloads` | 10 | Downloads/day (Free) |
| `free.daily_bandwidth` | 1 GB | Bandwidth/day (Free) |
| `free.concurrent` | 1 | Concurrent slots (Free) |
| `free.priority` | 2.0 | Queue priority (Free) |
| `pro.link_size` | 10 GB | Max total size per link (Pro) |
| `pro.daily_downloads` | 50 | Downloads/day (Pro) |
| `pro.daily_bandwidth` | 25 GB | Bandwidth/day (Pro) |
| `pro.concurrent` | 2 | Concurrent slots (Pro) |
| `pro.priority` | 1.0 | Queue priority (Pro) |
| `premium.link_size` | Unlimited | Max total size per link (Premium) |
| `premium.daily_downloads` | 200 | Downloads/day (Premium) |
| `premium.daily_bandwidth` | 100 GB | Bandwidth/day (Premium) |
| `premium.concurrent` | 3 | Concurrent slots (Premium) |
| `premium.priority` | 0.5 | Queue priority (Premium) |
| `admin.priority` | 0.2 | Queue priority (Admin; ahead of all tiers) |
| `album_size` | 10 | Files per delivered album |
| `failure_list_limit` | 10 | Failures shown before summarising |
| `default_magic_word` | `WOW` | Reply trigger word |
| `timer_media_default` | ON | TimerMedia initial state |
| `referral.daily_bonus` | 10 | Extra downloads/day per credited invite |
| `referral.bonus_days` | 30 | Validity window for each credit |
| `status.render_interval` | ~3 s | Min gap between content edits |
| `status.anim_interval` | ~1.2 s | Min gap between liveness-only edits |
| `session.encryption_key` | required | Key protecting stored cookies/sessions |

---

## 14. Explicitly out of scope

- Payment/billing processing (tiers are granted administratively or externally).
- Public web UI or API for end users.
- Content moderation or copyright enforcement policy.
- Analytics beyond the admin `/stats` view.
