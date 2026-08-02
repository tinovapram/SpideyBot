# SpideyBot — Telegram Media Downloader Bot

Download media from **TeraBox**, **Twitter/X**, **Instagram**, **TikTok**, **YouTube**, **Reddit**, **Bluesky**, **Pinterest**, **Spotify**, and 20+ more platforms — all from a single Telegram bot.

Send a link, get the files in chat. It's that simple.

---

## ✨ Highlights

- **30+ platforms** — TeraBox, YouTube, Twitter/X, Instagram, TikTok, Reddit, and more
- **Works anywhere** — send commands in private chat, groups, or channels
- **Real-time progress** — live progress bars with speed, ETA, and per-file status
- **Smart queue** — Premium users get priority; free users are fairly scheduled
- **User account mode** — log in with your own Telegram account for private content access
- **Cancel anytime** — cancel all downloads or target a specific one
- **Concurrent pipeline** — downloads and uploads run in parallel for maximum speed

---

## 📋 Supported Platforms

| Category | Platforms |
|:---|:---|
| **Cloud Storage** | TeraBox, Teraboxapp, Dubox, Nephobox, 1024tera |
| **Social Media** | Twitter/X, Instagram, TikTok, Facebook, Threads, LinkedIn |
| **Video** | YouTube, Dailymotion, Douyin, Kuaishou, CapCut |
| **Music** | Spotify, SoundCloud |
| **Image/Media** | Pinterest, Bluesky, Tumblr, Snapchat, Reddit |
| **Universal** | Any URL supported by [gallery-dl](https://github.com/mikf/gallery-dl) or [yt-dlp](https://github.com/yt-dlp/yt-dlp) |

---

## 🎯 How to Use

### Quick Start

1. Open your Telegram bot and send `/start`
2. Paste any supported link — the bot auto-detects it
3. Or use `/dl <link>` for explicit control

### Download Media

```
/dl https://terabox.com/s/1AbCdEfG
/dl https://x.com/user/status/123456789
/dl https://www.instagram.com/p/ABC123/
/dl https://www.reddit.com/r/submission/abc123
/dl https://youtube.com/watch?v=dQw4w9WgXcQ
```

You can also just **paste a link directly** — no command needed.

### Cancel Downloads

```
/cancel          — shows your active tasks (or cancels if only one)
/cancel 3        — cancel task #3 specifically
```

When you have multiple downloads running, `/cancel` lists them with IDs so you can pick which one to stop.

---

## 🔐 User Account (Session)

Log in with your **own Telegram account** to access private/restricted content that the bot alone can't reach.

### Session Commands

| Command | Description |
|:---|:---|
| `/login` | Start the login flow (phone → code → optional 2FA) |
| `/logout` | Revoke your saved session permanently |
| `/start` | Welcome message — also auto-connects your saved session |
| `/stop` | Disconnect your session without deleting it |

### How It Works

1. Send `/login` and follow the prompts (phone number → verification code → 2FA password if enabled)
2. Your session is **encrypted** and stored securely on disk
3. On next `/start`, your session auto-connects — no re-login needed
4. Use `/stop` to disconnect, or `/logout` to revoke entirely

### What Changes With a Session

| Feature | Without Session | With Session |
|:---|:---|:---|
| `/dl` in private chat | Processed by bot | **Delegated to your account** |
| `/dl` in groups | ✗ Not supported | ✓ Works from any chat |
| `/ping` | ✗ | ✓ Tests your session connection |
| Private content | ✗ | ✓ Access restricted media |

When you send `/dl` to the bot with an active session, the bot replies **"Processing via your user account"** and the download runs through your own Telegram client.

---

## 🌐 Commands Anywhere (Outgoing Commands)

When your session is active, these commands work **in any chat** — private, groups, or channels:

| Command | Description |
|:---|:---|
| `/dl <link>` | Download media (results delivered to your DM) |
| `/cancel` | Cancel your active/queued downloads |
| `/ping` | Test your session by pinging google.com |

Just type the command in any conversation and your account handles it. No need to open the bot chat.

---

## 📝 All Commands

### User Commands

| Command | Description |
|:---|:---|
| `/start` | Welcome message + auto-connect session |
| `/help` | Your tier, limits, session status, and full command list |
| `/dl <link>` | Download from any supported platform |
| `/cancel` | List or cancel your downloads |
| `/cancel <id>` | Cancel a specific download by task ID |
| `/login` | Log in with your Telegram account |
| `/logout` | Revoke your saved session |
| `/stop` | Disconnect your session (keeps it saved) |
| `/ping` | Test your session connection |

### Admin Commands

| Command | Description |
|:---|:---|
| `/addpremium <user> <days>` | Grant or extend Premium access |
| `/removepremium <user>` | Revoke Premium access |
| `/checkpremium <user>` | View detailed subscription status |

---

## 👑 User Tiers

| | Free | Premium | Admin |
|:---|:---|:---|:---|
| **Size Limit** | 100 MB | 1 GB | Unlimited |
| **Concurrent Downloads** | 1 | 5 | 5 |
| **Queue Priority** | Standard | Priority | Priority |

---

## 🔒 Security

- **Encrypted sessions** — your Telegram login session is encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC) before being stored on disk
- **No session in database** — sessions are stored as separate files, not in SQLite
- **Bot never sees your password** — 2FA passwords are handled directly by Telethon and never logged or stored
- **Revoke anytime** — `/logout` immediately deletes your session file and disconnects

---

## 🚀 Setup & Running

### Prerequisites

- **Python 3.11+**
- **FFmpeg** (for video/audio merging — install from [ffmpeg.org](https://ffmpeg.org))

### Quick Start (Local)

```bash
# 1. Clone and install
git clone https://github.com/your-username/MyBot.git
cd MyBot
pip install -r requirements.txt

# 2. Configure — copy and edit the env file
cp .env.example .env
# Fill in: TG_API_ID, TG_API_HASH, TG_BOT_TOKEN, TERABOX_COOKIE

# 3. Run
python main.py
```

### Docker (Recommended)

```bash
# Start
docker compose up --build -d

# View live logs
docker compose logs -f

# Stop
docker compose down
```

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|:---|:---|:---|
| `TG_API_ID` | ✅ | Get from [my.telegram.org](https://my.telegram.org) |
| `TG_API_HASH` | ✅ | Get from [my.telegram.org](https://my.telegram.org) |
| `TG_BOT_TOKEN` | ✅ | Get from [@BotFather](https://t.me/BotFather) |
| `TERABOX_COOKIE` | ⚠️ | Browser cookie containing `ndus` for TeraBox |
| `ADMIN_IDS` | Optional | Comma-separated Telegram User IDs for admin access |
| `SESSION_ENCRYPT_KEY` | Optional | Fernet key for session encryption (auto-generated if unset) |
| `MAX_CONCURRENT_DOWNLOADS` | Optional | Worker pool size (default: `20`) |

---

## 📁 Project Structure

```text
spideybot/
├── bot.py                  # Bot lifecycle & graceful shutdown
├── config.py               # Environment variables & validation
├── db.py                   # User tiers & subscription database
├── models.py               # SQLAlchemy ORM (users table)
├── user_sessions.py        # Encrypted session storage & client lifecycle
├── queue_manager.py        # Priority queue with concurrency limits
├── core/
│   ├── bot.py              # Entry point: wires everything together
│   └── handlers/
│       ├── user.py         # /dl, /start, /help, /cancel + cancel button
│       ├── login.py        # /login, /logout conversation flow
│       ├── admin.py        # /addpremium, /removepremium, /checkpremium
│       └── outgoing.py     # Outgoing commands (work in any chat)
├── downloaders/
│   ├── terabox_downloader.py   # TeraBox API client
│   ├── terabox_handler.py      # TeraBox download pipeline
│   ├── download_handler.py     # gallery-dl / Reddit orchestration
│   ├── reddit_downloader.py    # Reddit with refresh token auth
│   ├── gallerydl_downloader.py # gallery-dl CLI wrapper
│   ├── universal_downloader.py # yt-dlp + platform detection
│   └── site_downloaders/       # Per-site scrapers (20+ platforms)
└── utils/
    ├── files.py            # File download & sanitization
    ├── formatting.py       # Human-readable sizes & speeds
    └── task_progress.py    # Progress bar with speed, ETA, cancel button
```

---

## ❓ FAQ

**Q: Why is my TeraBox download failing?**
A: Make sure `TERABOX_COOKIE` is set with a valid `ndus` cookie from your browser. Cookies expire — refresh them if downloads stop working.

**Q: Can I use the bot in group chats?**
A: Yes! When your session is active, `/dl`, `/cancel`, and `/ping` all work in any group or channel.

**Q: Is my Telegram account safe?**
A: The bot uses Telethon (a well-known Telegram client library). Your session is encrypted at rest. You can revoke access anytime with `/logout`.

**Q: What happens if a download is too large?**
A: Free users are limited to 100 MB, Premium to 1 GB. The bot will tell you if a file exceeds your tier limit.

**Q: How do I cancel just one download?**
A: Send `/cancel` — if you have multiple active tasks, it lists them with IDs. Then send `/cancel <id>` to target a specific one.

---

## 📄 License

This project is open source. See the repository for license details.
