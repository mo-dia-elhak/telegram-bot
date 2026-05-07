import os
import re
import logging
import asyncio
import tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(
    r"https?://"
    r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"
    r"localhost|\d{1,3}(?:\.\d{1,3}){3})"
    r"(?::\d+)?"
    r"(?:/?|[/?]\S+)",
    re.IGNORECASE,
)

QUALITY_OPTIONS = [
    ("🏆 Best quality", "best"),
    ("📺 1080p", "1080"),
    ("📺 720p", "720"),
    ("📺 480p", "480"),
    ("📺 360p", "360"),
    ("📺 240p", "240"),
]

STATE_URL = "pending_url"
STATE_FORMAT = "pending_format"


def extract_url(text):
    match = URL_REGEX.search(text or "")
    return match.group(0) if match else None


def get_ydl_opts_mp3(output_path):
    return {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }


def get_ydl_opts_mp4(output_path, quality):
    if quality == "best":
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    else:
        fmt = (
            f"bestvideo[height<={quality}][ext=mp4]+"
            f"bestaudio[ext=m4a]/"
            f"best[height<={quality}][ext=mp4]/"
            f"best[height<={quality}]"
        )
    return {
        "format": fmt,
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }


async def download_media(url, opts):
    loop = asyncio.get_running_loop()

    def _download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)

    filename = await loop.run_in_executor(None, _download)
    path = Path(filename)
    search_dir = path.parent

    all_files = [f for f in search_dir.iterdir() if f.is_file()]
    if all_files:
        return max(all_files, key=lambda f: f.stat().st_mtime)

    raise FileNotFoundError(f"No downloaded file found in {search_dir}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to the Video Downloader Bot!*\n\n"
        "Send me a link from YouTube, TikTok, Instagram, Facebook, "
        "Reddit, Twitter/X, or any other platform.\n\n"
        "📎 Just paste a URL to get started!",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use this bot:*\n\n"
        "1️⃣ Send a video URL\n"
        "2️⃣ Choose the format: *MP4* or *MP3*\n"
        "3️⃣ If MP4, pick the quality\n"
        "4️⃣ Wait for your file ✅",
        parse_mode="Markdown",
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = extract_url(update.message.text)
    if not url:
        await update.message.reply_text("❌ No valid URL found. Please send a direct link.")
        return

    context.user_data[STATE_URL] = url

    keyboard = [
        [
            InlineKeyboardButton("🎬 MP4 (Video)", callback_data="fmt:mp4"),
            InlineKeyboardButton("🎵 MP3 (Audio)", callback_data="fmt:mp3"),
        ]
    ]
    await update.message.reply_text(
        f"🔗 URL detected!\n\n`{url}`\n\n*Choose the download format:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, fmt = query.data.split(":", 1)
    context.user_data[STATE_FORMAT] = fmt

    if fmt == "mp3":
        await query.edit_message_text("⏳ Downloading MP3, please wait…")
        await perform_download(query, context)
    else:
        keyboard = [
            [InlineKeyboardButton(label, callback_data=f"quality:{value}")]
            for label, value in QUALITY_OPTIONS
        ]
        await query.edit_message_text(
            "📺 *Choose the video quality:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, quality = query.data.split(":", 1)
    context.user_data["quality"] = quality

    label = next((l for l, v in QUALITY_OPTIONS if v == quality), quality)
    await query.edit_message_text(f"⏳ Downloading MP4 ({label}), please wait…")
    await perform_download(query, context)


async def perform_download(query, context: ContextTypes.DEFAULT_TYPE):
    url = context.user_data.get(STATE_URL)
    fmt = context.user_data.get(STATE_FORMAT, "mp4")
    quality = context.user_data.get("quality", "best")
    chat_id = query.message.chat_id

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = os.path.join(tmpdir, "%(title)s.%(ext)s")

        try:
            if fmt == "mp3":
                opts = get_ydl_opts_mp3(output_template)
            else:
                opts = get_ydl_opts_mp4(output_template, quality)

            file_path = await download_media(url, opts)

        except yt_dlp.utils.DownloadError as e:
            logger.error("Download error: %s", e)
            await context.bot.send_message(
                chat_id,
                f"❌ *Download failed.*\n\nThe video may be private or geo-restricted.\n\n`{e}`",
                parse_mode="Markdown",
            )
            return
        except FileNotFoundError as e:
            logger.error("File not found: %s", e)
            await context.bot.send_message(
                chat_id,
                "❌ *Download failed.* Could not locate the downloaded file.",
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            logger.exception("Unexpected error")
            await context.bot.send_message(
                chat_id, f"❌ Unexpected error: `{e}`", parse_mode="Markdown"
            )
            return

        if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            await context.bot.send_message(
                chat_id,
                "⚠️ File exceeds Telegram's 50 MB limit.\nPlease try a lower quality.",
            )
            return

        try:
            with open(file_path, "rb") as f:
                if fmt == "mp3":
                    await context.bot.send_audio(
                        chat_id,
                        audio=f,
                        filename=file_path.name,
                        caption="🎵 Here's your MP3!",
                    )
                else:
                    await context.bot.send_video(
                        chat_id,
                        video=f,
                        filename=file_path.name,
                        caption="🎬 Here's your video!",
                        supports_streaming=True,
                    )
        except Exception as e:
            logger.exception("Failed to send file")
            await context.bot.send_message(
                chat_id, f"❌ Could not send the file: `{e}`", parse_mode="Markdown"
            )
            return

    for key in (STATE_URL, STATE_FORMAT, "quality"):
        context.user_data.pop(key, None)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤔 Send me a video link and I'll handle the rest!")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(URL_REGEX), handle_url))
    app.add_handler(CallbackQueryHandler(handle_format_choice, pattern=r"^fmt:"))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^quality:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
