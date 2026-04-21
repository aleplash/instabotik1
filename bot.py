import logging
import re
import asyncio
import os
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

INSTAGRAM_PATTERN = re.compile(
    r'https?://(www\.)?instagram\.com/reels?/[^\s]+'
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет!\n\n"
        "Этот бот предназначен для конвертации видео из инстаграма в телеграм.\n"
        "Ты можешь использовать его и тут, а можешь добавить его в групповой чат.\n\n"
        "Отправь ему ссылку формата https://www.instagram.com/reels/xxxxxxx и получи результат"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    new_links = []
    for match in INSTAGRAM_PATTERN.finditer(text):
        original = match.group(0)
        converted = re.sub(r'(https?://)(?:www\.)?instagram\.com', r'\1kkclip.com', original)
        new_links.append(converted)

    if not new_links:
        return

    await message.reply_text("\n".join(new_links))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("Укажи BOT_TOKEN в переменных окружения!")

    Thread(target=run_health_server, daemon=True).start()
    print("Бот запущен...")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
