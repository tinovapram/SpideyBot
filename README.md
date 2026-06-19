# SpideyBot - Multi-purpose Telegram Bot

SpideyBot is a multi-purpose assistant Telegram bot built using the [Telethon](https://github.com/LonamiWebs/Telethon) library. Its initial feature module is a premium media downloader which resolves and downloads files from **TeraBox** shared links and hundreds of other galleries/video hosts supported by **gallery-dl** (e.g., YouTube, Twitter, Imgur, Reddit, Instagram, etc.).

---

## Key Features

1. **Unified Download Command (`/dl <link>`)**
   - Resolves, downloads, and uploads media files directly to the chat.
   - Raw links sent directly to the bot trigger a helpful prompt reminding users to use the `/dl` command.
2. **Priority-Based Concurrency Queue**
   - An asynchronous priority queue ensures Premium and Admin tasks are processed before Free tasks.
   - Preserves FIFO (First-In-First-Out) scheduling for tasks belonging to the same tier.
   - Uses a pool of background workers (configurable size) to handle downloads concurrently.
3. **In-Memory User Caching**
   - Keeps user membership state (Free, Premium, Admin) in an in-memory cache to eliminate redundant SQLite disk reads on every user interaction.
4. **Subprocess Size Monitoring**
   - Continuously monitors download folder sizes in real-time during `gallery-dl` processes. If any download exceeds the tier limits, it automatically terminates the process, purges downloaded files, and alerts the user.

---

## User Tiers & Limits

| Privilege / limit | Free Tier | Premium Tier | Admin Tier |
| :--- | :--- | :--- | :--- |
| **Download Size Limit** | 100 MB total | 1 GB total | **Unlimited** |
| **Concurrent Downloads** | Max 1 active | Max 5 active | Max 5 active |
| **Queue Priority** | Standard (Low) | Priority (High) | Priority (High) |
| **Admin Commands** | No | No | **Yes** |

---

## Commands

### General Commands
- `/start` - Start the bot and get a welcome message.
- `/help` - View current membership status, limits, and usage instructions (admins also see admin commands).
- `/dl <link>` - Queue a download request for TeraBox or any supported gallery-dl site.

### Admin Commands (Admin Tier only)
Admins are defined via the `ADMIN_IDS` environment variable.
- `/addpremium <username_or_userid> <days>` - Grant or extend a user's Premium subscription.
- `/removepremium <username_or_userid>` - Revoke a user's Premium subscription.
- `/checkpremium <username_or_userid>` - View the detailed subscription status of any user (displays `Admin Bypass` status for admins).

---

## Prerequisites

- **Python 3.8+**
- **FFmpeg** (installed on the host system and added to PATH, required by `gallery-dl` and `yt-dlp` for media stitching).

---

## Configuration

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
2. Open the `.env` file and configure:
   - **`TG_API_ID`** & **`TG_API_HASH`**: Telegram developer API keys (from [my.telegram.org](https://my.telegram.org)).
   - **`TG_BOT_TOKEN`**: Telegram bot token (from [@BotFather](https://t.me/BotFather)).
   - **`TERABOX_COOKIE`**: Valid web cookie (containing `ndus`) extracted from your browser.
   - **`ADMIN_IDS`**: Comma-separated list of Telegram User IDs authorized as admins (e.g., `ADMIN_IDS="1234567,9876543"`).
   - **`MAX_CONCURRENT_DOWNLOADS`**: The size of the bot's global concurrent worker pool (default `20`).

---

## Running the Bot

### Local Execution
```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python bot.py
```

### Docker Execution (Recommended)
You can run the bot inside a Docker container. FFmpeg, Deno, and all required Python dependencies are pre-installed in the Docker image.

#### Using Docker Compose (Simplest)
1. Start the container in the background:
   ```bash
   docker compose up --build -d
   ```
2. Stop the container:
   ```bash
   docker compose down
   ```

#### Manual Docker CLI
1. Build the Docker image:
   ```bash
   docker build -t spideybot .
   ```
2. Run the container (make sure to mount `/app/data` to a host volume to persist SQLite databases):
   ```bash
   docker run -d \
     --name spideybot-container \
     --env-file .env \
     -v ./data:/app/data \
     spideybot
   ```
