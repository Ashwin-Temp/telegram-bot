import os
import re
import logging
import asyncio
import signal
import time
import tempfile
from datetime import datetime, timedelta
from typing import Dict, Optional

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait
import yt_dlp
from dotenv import load_dotenv

# ----------------- Setup ----------------- #
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 60))

if not CHANNEL_ID.startswith("@") and not CHANNEL_ID.startswith("-100"):
    CHANNEL_ID = f"@{CHANNEL_ID}"

shutdown_flag = False
active_tasks = {}
user_cooldowns = {}

app = Client(
    "media_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ----------------- Utility Functions ----------------- #
def is_valid_url(url: str) -> bool:
    yt_pattern = r'(youtube|youtu\.be)'
    insta_pattern = r'(instagram\.com|instagr\.am)'
    return bool(re.search(yt_pattern, url, re.IGNORECASE) or re.search(insta_pattern, url, re.IGNORECASE))

def format_size(bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024.0:
            return f"{bytes:.1f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.1f} TB"

async def update_progress_message(user_id: int, text: str):
    try:
        if user_id in active_tasks and active_tasks[user_id].get('status_msg'):
            await app.edit_message_text(
                chat_id=active_tasks[user_id]['status_msg'].chat.id,
                message_id=active_tasks[user_id]['status_msg'].id,
                text=f"{text}\n\n⏳ Please wait, time depends on file size...",
                parse_mode=ParseMode.HTML
            )
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await update_progress_message(user_id, text)
    except Exception as e:
        logger.warning(f"Progress update failed for user {user_id}: {e}")

async def check_channel_membership(user_id: int) -> bool:
    try:
        if not CHANNEL_ID:
            return True
        member = await app.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in [
            ChatMemberStatus.LEFT,
            ChatMemberStatus.BANNED,
            ChatMemberStatus.RESTRICTED
        ]
    except Exception as e:
        logger.warning(f"Channel membership check failed for user {user_id}: {e}")
        return False

# ----------------- Downloader Core ----------------- #
async def download_media(url: str, user_id: int) -> Optional[str]:
    if shutdown_flag:
        return None

    last_time = 0
    update_count = 0
    animation_states = ["⏳", "⏳.", "⏳..", "⏳..."]
    tmp_path = os.path.join(tempfile.gettempdir(), f"vid_{user_id}_{int(time.time())}.mp4")

    def hook(d: dict):
        nonlocal last_time, update_count
        if d['status'] not in ['downloading', 'finished']:
            return
        now = time.time()
        if d['status'] != 'finished' and d.get('total_bytes', 0) > 1024 * 1024 and now - last_time < 0.5:
            return
        last_time = now
        update_count += 1
        try:
            if d['status'] == 'finished':
                text = f"⬇️ <b>Download Complete</b>\n\n<b>Downloaded:</b> <code>{format_size(d.get('total_bytes', 0))}</code>"
            else:
                downloaded = format_size(d.get('downloaded_bytes', 0))
                total = format_size(d.get('total_bytes', 0))
                anim = animation_states[update_count % len(animation_states)]
                text = f"⬇️ <b>Downloading{anim}</b>\n\n<b>Downloaded:</b> <code>{downloaded} / {total}</code>\n⏱️ Please wait, time depends on file size."
            asyncio.create_task(update_progress_message(user_id, text))
        except Exception as e:
            logger.warning(f"Progress hook failed: {e}")

    # ✅ Basic yt-dlp options
    opts = {
        'format': 'bestvideo+bestaudio/best',
        'outtmpl': tmp_path,
        'progress_hooks': [hook],
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4'
        }],
    }

    # ✅ Add Instagram cookies only if it's an IG URL
    if "instagram.com" in url or "instagr.am" in url:
        opts['cookiefile'] = 'instagram_cookies.txt'

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return tmp_path
    except Exception as e:
        logger.error(f"Download error: {e}")
        await update_progress_message(user_id, f"❌ Download failed: {str(e)}")
        return None

async def upload_media(client: Client, path: str, chat_id: int, user_id: int) -> bool:
    try:
        sent = await client.send_video(chat_id, path, caption="✅ Here's your video! 🥳")
        return sent
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        return None
    finally:
        try:
            os.remove(path)
        except Exception as e:
            logger.warning(f"Failed to delete file {path}: {e}")

# ----------------- Command Handlers ----------------- #
@app.on_message(filters.command("start") & filters.private)
async def start(_, msg: Message):
    await msg.reply(
        "👋 Welcome to the YouTube & Instagram Video Downloader Bot!\n\nSend a video link to get started.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Join Channel", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")]
        ])
    )

@app.on_message(filters.private & ~filters.command("start"))
async def handle_video(client: Client, msg: Message):
    user_id = msg.from_user.id
    if shutdown_flag or not msg.text:
        return

    status_msg = await msg.reply("🔄 Starting download...", parse_mode=ParseMode.HTML)

    if not is_valid_url(msg.text):
        await status_msg.edit("❌ Please send a valid YouTube or Instagram URL.")
        return

    if user_id in user_cooldowns and user_cooldowns[user_id] > datetime.now():
        remain = int((user_cooldowns[user_id] - datetime.now()).total_seconds())
        await status_msg.edit(f"⏳ Please wait {remain} seconds before next request.")
        return

    if not await check_channel_membership(user_id):
        await status_msg.edit(
            "🔒 Join the channel first!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")]
            ])
        )
        return

    active_tasks[user_id] = {'status_msg': status_msg, 'running': True}
    user_cooldowns[user_id] = datetime.now() + timedelta(seconds=COOLDOWN_SECONDS)

    path = await download_media(msg.text, user_id)
    if not path or not os.path.exists(path):
        await status_msg.edit("❌ Failed to download media.")
        active_tasks.pop(user_id, None)
        return

    await update_progress_message(user_id, "⬆️ Uploading...")
    sent_video = await upload_media(client, path, msg.chat.id, user_id)
    if not sent_video:
        await msg.reply("❌ Failed to upload file.")
        active_tasks.pop(user_id, None)
        return

    # Delete all status messages
    try:
        await status_msg.delete()
    except:
        pass
    try:
        await app.delete_messages(chat_id=msg.chat.id, message_ids=[active_tasks[user_id]['status_msg'].id])
    except:
        pass

    active_tasks.pop(user_id, None)

# ----------------- Main ----------------- #
if __name__ == "__main__":
    async def handle_sigint(signum, frame):
        global shutdown_flag
        shutdown_flag = True
        logger.info("Gracefully shutting down...")
        for user_id in list(active_tasks.keys()):
            await update_progress_message(user_id, "🛑 Bot is shutting down, download cancelled.")
        active_tasks.clear()
        await app.stop()

    signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(handle_sigint(s, f)))
    logger.info("Bot starting...")
    app.run()
    logger.info("Bot stopped.")
