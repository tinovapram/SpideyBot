# SpideyBot - Multi-purpose Telegram Bot

SpideyBot is a high-performance, asynchronous Telegram bot built with [Telethon](https://github.com/LonamiWebs/Telethon). Its primary feature is a premium media downloader that resolves and downloads files from **TeraBox** shared links, plus hundreds of other galleries and video hosts supported by **gallery-dl** (YouTube, Twitter/X, Instagram, TikTok, Reddit, etc.) with custom fallbacks for platforms that need special handling.

---

## Key Features

| Feature | Description |
|:---|:---|
| **Unified Download (`/dl <link>`)** | Resolves, downloads, and uploads media directly to the chat. Supports TeraBox, Twitter/X, Instagram, TikTok, YouTube, Reddit, Bluesky, Pinterest, Spotify, and 20+ more platforms. |
| **Cancel Button** | Inline ❌ Cancel button appears on all progress messages during download/upload — click it at any time to abort. |
| **Priority-Based Queue** | Async priority queue with fair-share scheduling. Premium/Admin tasks are processed before Free tasks; FIFO within the same tier. |
| **Pipeline Downloads** | TeraBox downloads use a producer/consumer pipeline — files are downloaded and uploaded concurrently for maximum throughput. |
| **Structured Logging** | All 14 modules use [structlog](https://www.structlog.org/) with key-value pairs for clean, searchable, JSON-compatible logs. |
| **Graceful Shutdown** | SIGINT/SIGTERM handlers drain workers, close sessions, and disconnect the bot cleanly. Works on both Linux and Windows. |
| **In-Memory User Cache** | User membership state is cached in memory to eliminate redundant SQLite disk reads during rate-limit checks. |
| **Subprocess Size Monitoring** | Real-time folder size monitoring during gallery-dl processes. Downloads exceeding tier limits are automatically killed and cleaned up. |

---

## User Tiers & Limits

| Privilege / Limit | Free Tier | Premium Tier | Admin Tier |
| :--- | :--- | :--- | :--- |
| **Download Size Limit** | 100 MB total | 1 GB total | **Unlimited** |
| **Concurrent Downloads** | Max 1 active | Max 5 active | Max 5 active |
| **Queue Priority** | Standard (Low) | Priority (High) | Priority (High) |
| **Admin Commands** | No | No | **Yes** |

---

## Commands

### General Commands
| Command | Description |
|:---|:---|
| `/start` | Start the bot and get a welcome message |
| `/help` | View membership status, limits, and usage instructions |
| `/dl <link>` | Queue a download request (TeraBox, gallery-dl, Reddit, etc.) |
| `/cancel` | Cancel all your queued/active downloads |

### Admin Commands (Admin Tier only)
Admins are defined via the `ADMIN_IDS` environment variable.
| Command | Description |
|:---|:---|
| `/addpremium <user> <days>` | Grant or extend a user's Premium subscription |
| `/removepremium <user>` | Revoke a user's Premium subscription |
| `/checkpremium <user>` | View detailed subscription status of any user |

---

## Project Structure

```text
MyBot/
├── main.py                     # Root entry point
├── requirements.txt            # Python package dependencies
├── Dockerfile                  # Multi-stage image (Python 3.11 + Deno + FFmpeg)
├── docker-compose.yml          # Container service definition & volume mapping
├── .env                        # Environment variables (not in Git)
├── gallery-dl.json             # gallery-dl configuration
├── yt-dlp.conf                 # Global yt-dlp options
├── data/                       # SQLite DB (mounted to container volume)
├── downloads/                  # Temporary downloads (gitignored)
├── spideybot/                  # Main package
│   ├── __init__.py
│   ├── __main__.py             # Package execution (python -m spideybot)
│   ├── bot.py                  # Bot lifecycle, handler registration, graceful shutdown
│   ├── config.py               # Environment variables, validation, constants
│   ├── db.py                   # SQLite database + in-memory user tier cache
│   ├── logging_config.py       # structlog setup on stdlib logging backbone
│   ├── queue_manager.py        # Async priority queue with per-user concurrency limits
│   ├── core/
│   │   ├── handlers/
│   │   │   ├── user.py         # /dl, /start, /help, /cancel, cancel-button callback
│   │   │   └── admin.py        # /addpremium, /removepremium, /checkpremium
│   ├── downloaders/
│   │   ├── download_handler.py # gallery-dl / Reddit / universal download orchestration
│   │   ├── terabox_downloader.py  # TeraBox API client (resolve, transfer, download links)
│   │   ├── terabox_handler.py  # TeraBox pipeline (resolve → download → upload → send)
│   │   ├── reddit_downloader.py   # Reddit downloader with multi-client refresh token auth
│   │   ├── gallerydl_downloader.py # gallery-dl CLI wrapper
│   │   ├── gallerydl_handler.py   # gallery-dl file pipeline with size monitoring
│   │   ├── universal_downloader.py # Platform detection + yt-dlp integration
│   │   └── site_downloaders/   # Per-site custom scrapers
│   │       ├── twitter.py, tiktok.py, youtube.py, bluesky.py, ...
│   └── utils/
│       ├── files.py            # Filename sanitization, async TeraBox file download
│       ├── formatting.py       # Human-readable size/speed formatting
│       ├── progress.py         # Upload progress callback wrapper
│       └── task_progress.py    # Telegram progress bar with speed, ETA, and cancel button
```

---

## Prerequisites

- **Python 3.11+**
- **FFmpeg** (installed on the host system and added to PATH, required by gallery-dl and yt-dlp for media stitching)

---

## Configuration

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` and configure:

| Variable | Required | Description |
|:---|:---|:---|
| `TG_API_ID` | ✅ | Telegram API ID (from [my.telegram.org](https://my.telegram.org)) |
| `TG_API_HASH` | ✅ | Telegram API Hash |
| `TG_BOT_TOKEN` | ✅ | Telegram bot token (from [@BotFather](https://t.me/BotFather)) |
| `TERABOX_COOKIE` | ⚠️ | TeraBox cookie containing `ndus` from your browser session |
| `ADMIN_IDS` | Optional | Comma-separated Telegram User IDs for admin access |
| `MAX_CONCURRENT_DOWNLOADS` | Optional | Global worker pool size (default: `20`) |
| `REDDIT_PRAW_CLIENT_ID` | Optional | Reddit app client ID for PRAW auth |
| `REDDIT_PRAW_CLIENT_SECRET` | Optional | Reddit app client secret |
| `REDDIT_GDL_REFRESH_TOKEN` | Optional | Reddit refresh token (for gallery-dl auth) |

---

## Running the Bot

### Local Execution
```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python main.py
```

### Docker Execution (Recommended)
FFmpeg, Deno, and all required Python dependencies are pre-installed in the Docker image.

#### Using Docker Compose (Simplest)
```bash
# Start in background
docker compose up --build -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

#### Manual Docker CLI
```bash
# Build
docker build -t spideybot .

# Run (mount /app/data to persist the SQLite database)
docker run -d \
  --name spideybot-container \
  --env-file .env \
  -v ./data:/app/data \
  spideybot
```

---

## Architecture Highlights

### Structured Logging
All modules use `structlog` with key-value pairs for production-grade observability:
```python
logger.info("Download completed", filename="video.mp4", size_mb=45.2, duration_s=12)
```

### Graceful Shutdown
SIGINT/SIGTERM triggers a 5-step cleanup: stop accepting tasks → cancel queued tasks → drain workers → close HTTP sessions → disconnect Telegram client.

### Cross-Platform
Runs on both Linux (Docker) and Windows (local dev). Signal handling adapts automatically: `loop.add_signal_handler()` on Linux, `signal.signal()` on Windows.
