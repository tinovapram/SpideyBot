"""
SpideyBot — Telegram Bot Handlers.

Contains all Telegram command handlers and the bot startup logic.
Configuration is loaded from spideybot.config, database access via spideybot.db.
"""

import re
import time
import logging
from telethon import TelegramClient, events

from spideybot import config
from spideybot import db
from spideybot.queue_manager import DownloadQueueManager
from spideybot.downloaders.terabox_downloader import TeraBoxDownloader

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Initialization ──────────────────────────────────────────────────

# Validate Telegram config and get integer API_ID
API_ID = config.validate_telegram_config()

# Initialize the Telethon client
logger.info("Initializing Telegram client...")
bot = TelegramClient('bot_session', API_ID, config.TG_API_HASH)

# Initialize TeraBox Downloader
tb_downloader = None
if config.TERABOX_COOKIE:
    try:
        tb_downloader = TeraBoxDownloader(
            cookie=config.TERABOX_COOKIE,
            js_token=config.TERABOX_JSTOKEN,
            bds_token=config.TERABOX_BDSTOKEN
        )
        logger.info("TeraBox downloader initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize TeraBox downloader: {e}")
else:
    logger.warning("TERABOX_COOKIE is not set. TeraBox features will be unavailable.")

# Initialize Queue Manager
download_manager = DownloadQueueManager(bot, tb_downloader, max_concurrent=config.MAX_CONCURRENT_DOWNLOADS)


# ─── Helpers ─────────────────────────────────────────────────────────

def is_premium_user(user_id: int) -> bool:
    """Check if a user is an admin or has active premium status."""
    return (user_id in config.ADMIN_IDS) or db.is_user_premium(user_id)


def extract_terabox_links(text: str) -> list:
    """Extract and validate potential TeraBox URLs from text."""
    if not text:
        return []
    url_pattern = re.compile(r'https?://[^\s]+', re.IGNORECASE)
    urls = url_pattern.findall(text)
    terabox_urls = []

    for url in urls:
        url_clean = url.rstrip('.,;!?)"\'')
        if config.is_terabox_url(url_clean):
            try:
                TeraBoxDownloader.parse_surl(url_clean)
                terabox_urls.append(url_clean)
            except Exception:
                pass
    return terabox_urls


# ─── Command Handlers ────────────────────────────────────────────────

# Command: /start
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """Send a welcome message when the command /start is issued."""
    user = await event.get_sender()
    user_id = event.sender_id
    username = user.username if user else None

    # Save/update user record
    db.save_or_update_user(user_id, username)

    first_name = user.first_name if user else "there"
    welcome_text = (
        f"Hello {first_name}!\n\n"
        "I am SpideyBot, a multi-purpose assistant bot.\n"
        "My current features include a premium media downloader.\n\n"
        "Send me any TeraBox share link, or use the `/dl <link>` command for gallery-dl supported sites (YouTube, Twitter, Imgur, etc.).\n\n"
        "Use `/help` to see limits, premium status, and admin commands."
    )
    logger.info(f"User {user_id} triggered /start")
    await event.respond(welcome_text)


# Command: /help
@bot.on(events.NewMessage(pattern='/help'))
async def help_handler(event):
    """Send a help message detailing user tier limits."""
    user = await event.get_sender()
    user_id = event.sender_id
    username = user.username if user else None

    # Save/update user record
    db.save_or_update_user(user_id, username)

    is_admin = user_id in config.ADMIN_IDS
    is_premium = is_premium_user(user_id)

    if is_admin:
        status_text = "👑 Admin Tier"
        limit_desc = "Unlimited download size, 5 concurrent downloads, priority queue processing"
    else:
        status_text = "✨ Premium Tier" if is_premium else "👤 Free Tier"
        limit_desc = "1GB max size, 5 concurrent downloads, priority queue processing" if is_premium else "100MB max size, 1 concurrent download"

    help_text = (
        "**SpideyBot Help & Information**\n\n"
        f"Your Membership: **{status_text}**\n"
        f"Limits: `{limit_desc}`\n\n"
        "**Commands:**\n"
        "• `/dl <link>` - Download files from TeraBox or any supported gallery-dl site (Twitter, Imgur, Reddit, etc.)\n"
        "• `/start` - Start the bot and get a welcome message\n"
        "• `/help` - Show this help page\n\n"
        "Just send or paste any valid TeraBox link, or use `/dl <link>` for any other site, and I'll add the task to the queue."
    )

    if is_admin:
        help_text += (
            "\n\n**Admin Commands:**\n"
            "• `/addpremium <username_or_userid> <days>` - Grant premium tier\n"
            "• `/removepremium <username_or_userid>` - Revoke premium tier\n"
            "• `/checkpremium <username_or_userid>` - Check user status"
        )

    logger.info(f"User {user_id} triggered /help")
    await event.respond(help_text)


# Command: /dl {link}
@bot.on(events.NewMessage(pattern=r'/dl(?:\s+(https?://\S+))?'))
async def dl_command_handler(event):
    """Queue a download request from a /dl command."""
    user = await event.get_sender()
    user_id = event.sender_id
    username = user.username if user else None

    # Save/update user record
    db.save_or_update_user(user_id, username)

    link = event.pattern_match.group(1)
    if not link:
        await event.reply("⚠️ Please specify a valid URL.\nUsage: `/dl <link>`")
        return

    # Create single status message
    status_msg = await event.reply("⏳ **SpideyBot:** Queueing your download request...")

    is_admin = user_id in config.ADMIN_IDS
    is_premium = is_premium_user(user_id)

    status = await download_manager.add_task(user_id, event, link, is_premium, is_admin, status_msg)
    if status == "user_limit":
        limit = config.get_concurrent_limit(is_premium)
        await status_msg.edit(
            f"⚠️ **SpideyBot: Limit Exceeded.** You already have {limit} active download(s).\n"
            "Please wait for your active downloads to finish."
        )
    elif status == "global_limit":
        await status_msg.edit(
            "⚠️ **SpideyBot: Server Busy.** All free download slots are currently full.\n"
            "Please try again in a few minutes, or upgrade to Premium for instant access."
        )


# Admin Command: /addpremium <username_or_userid> <days>
@bot.on(events.NewMessage(pattern=r'/addpremium\s+(\S+)\s+(\d+)'))
async def add_premium_handler(event):
    """Grant premium membership to a user for a number of days."""
    sender_id = event.sender_id
    if sender_id not in config.ADMIN_IDS:
        await event.reply("❌ You are not authorized to use this command.")
        return

    target = event.pattern_match.group(1)
    try:
        days = int(event.pattern_match.group(2))
    except ValueError:
        await event.reply("❌ Days must be a valid integer.")
        return

    if target.isdigit():
        target_uid = int(target)
        expiry = db.add_premium_by_id(target_uid, days)
        expiry_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry))
        await event.reply(
            f"✅ **Premium Granted:**\n"
            f"• User ID: `{target_uid}`\n"
            f"• Duration: `{days}` days\n"
            f"• Expiry: `{expiry_str}`"
        )
    else:
        success, uid, expiry = db.add_premium_by_username(target, days)
        if success:
            expiry_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry))
            await event.reply(
                f"✅ **Premium Granted:**\n"
                f"• Username: `{target}` (ID: `{uid}`)\n"
                f"• Duration: `{days}` days\n"
                f"• Expiry: `{expiry_str}`"
            )
        else:
            await event.reply(
                f"❌ **User not found:** The username `{target}` is not in the bot's database.\n"
                "They must start/interact with the bot at least once."
            )


# Admin Command: /removepremium <username_or_userid>
@bot.on(events.NewMessage(pattern=r'/removepremium\s+(\S+)'))
async def remove_premium_handler(event):
    """Revoke premium membership from a user."""
    sender_id = event.sender_id
    if sender_id not in config.ADMIN_IDS:
        await event.reply("❌ You are not authorized to use this command.")
        return

    target = event.pattern_match.group(1)
    success, msg, uid = db.remove_premium(target)
    if success:
        await event.reply(f"✅ Successfully revoked premium status for `{target}` (ID: `{uid}`).")
    else:
        await event.reply(f"❌ Failed to revoke premium: {msg}")


# Admin Command: /checkpremium <username_or_userid>
@bot.on(events.NewMessage(pattern=r'/checkpremium\s+(\S+)'))
async def check_premium_handler(event):
    """Check membership status of any user."""
    sender_id = event.sender_id
    if sender_id not in config.ADMIN_IDS:
        await event.reply("❌ You are not authorized to use this command.")
        return

    target = event.pattern_match.group(1)

    # Check if target is an admin (using db.find_user_by_username instead of inline SQL)
    is_target_admin = False
    if target.isdigit():
        if int(target) in config.ADMIN_IDS:
            is_target_admin = True
    else:
        uid = db.find_user_by_username(target.lstrip('@'))
        if uid and uid in config.ADMIN_IDS:
            is_target_admin = True

    if is_target_admin:
        await event.reply(f"👤 User: `{target}`\n✨ Status: Premium (Admin Bypass)")
    else:
        status_msg = db.check_user_premium_status(target)
        await event.reply(status_msg)


# Default message handler (Remind user to use /dl command)
@bot.on(events.NewMessage)
async def default_message_handler(event):
    """Detect raw TeraBox links and remind user to use /dl."""
    # Skip command messages
    if event.text and event.text.startswith('/'):
        return

    links = extract_terabox_links(event.text)
    if not links:
        return

    logger.info(f"Detected raw TeraBox link from user {event.sender_id}. Prompting for /dl command.")
    await event.reply(
        "⚠️ **To download, please use the `/dl` command.**\n"
        "Example: `/dl <terabox_link>`"
    )


# ─── Bot Lifecycle ───────────────────────────────────────────────────

async def start_bot():
    """Initialize services and start the bot."""
    # Initialize database
    db.init_db()

    # Start priority queue worker threads/tasks
    download_manager.start_workers()

    # Start the client using the bot token
    await bot.start(bot_token=config.TG_BOT_TOKEN)  # type: ignore
    logger.info("Bot is running and listening for messages.")
    await bot.run_until_disconnected()


def main():
    """Entry point for the bot."""
    logger.info("Starting Telegram bot...")
    try:
        bot.loop.run_until_complete(start_bot())
    except Exception as e:
        logger.exception(f"An error occurred while running the bot: {e}")
