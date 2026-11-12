import asyncio
import html
import logging
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import httpx
import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("instabot")

# ---------- настройки (переменные окружения) ----------
# BOT_TOKEN            — токен бота (обязательно)
# SEND_AS_DOCUMENT=1   — присылать документом (без сжатия), а не обычным видео/фото
# INSTAGRAM_COOKIES    — содержимое cookies.txt (формат Netscape) для yt-dlp, необязательно
# FALLBACK_HOST        — запасной сервис, если yt-dlp не справился (по умолчанию kkclip.com)
SEND_AS_DOCUMENT = os.environ.get("SEND_AS_DOCUMENT", "").lower() in ("1", "true", "yes")
FALLBACK_HOST = os.environ.get("FALLBACK_HOST", "kkclip.com").strip()
MAX_BYTES = 50 * 1024 * 1024  # лимит Telegram на отправку файлов ботом
MAX_ITEMS = 10  # максимум файлов из одной карусели

INSTAGRAM_PATTERN = re.compile(
    r"https?://(?:www\.)?instagram\.com(/(?:share/)?(?:reels?|p|tv)/[A-Za-z0-9_-]+)[^\s]*"
)
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
BOT_UA = "TelegramBot (like TwitterBot)"


class TooBig(Exception):
    pass


def find_links(text: str) -> list[tuple[str, str]]:
    """Возвращает [(полная ссылка, путь вида /reel/XXXX)] без повторов."""
    seen, result = set(), []
    for m in INSTAGRAM_PATTERN.finditer(text):
        path = m.group(1)
        if path not in seen:
            seen.add(path)
            result.append((m.group(0), path))
    return result


def collect_files(folder: Path) -> list[Path]:
    files = [
        p for p in sorted(folder.iterdir())
        if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT and p.stat().st_size > 0
    ]
    return files[:MAX_ITEMS]


# ---------- способ 1: yt-dlp ----------
def _cookie_file() -> str | None:
    cookies = os.environ.get("INSTAGRAM_COOKIES")
    if not cookies:
        return None
    path = Path(tempfile.gettempdir()) / "instagram_cookies.txt"
    path.write_text(cookies)
    return str(path)


def download_with_ytdlp(url: str, folder: Path) -> list[Path]:
    opts = {
        "outtmpl": str(folder / "%(autonumber)02d_%(id)s.%(ext)s"),
        # готовый mp4 одним файлом — чтобы не нужен был ffmpeg
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "playlistend": MAX_ITEMS,
        "socket_timeout": 30,
        "retries": 2,
    }
    cookiefile = _cookie_file()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    return collect_files(folder)


# ---------- способ 2: запасной сервис (og:video / og:image) ----------
META_RE = re.compile(r"<meta\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def parse_og(page: str) -> dict[str, list[str]]:
    """Достаёт og:video и og:image из HTML."""
    found: dict[str, list[str]] = {"video": [], "image": []}
    for tag in META_RE.findall(page):
        attrs = {k.lower(): (a or b) for k, a, b in ATTR_RE.findall(tag)}
        prop = (attrs.get("property") or attrs.get("name") or "").lower()
        content = html.unescape(attrs.get("content") or "")
        if not content.startswith("http"):
            continue
        if prop in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
            kind = "video"
        elif prop in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image"):
            kind = "image"
        else:
            continue
        if content not in found[kind]:
            found[kind].append(content)
    return found


async def _save_stream(client: httpx.AsyncClient, url: str, folder: Path, index: int) -> Path | str:
    """Скачивает url. Если это медиа — возвращает путь к файлу, если HTML — его текст."""
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype.startswith("video/"):
            ext = ".mp4"
        elif ctype.startswith("image/"):
            ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
        else:
            return (await resp.aread()).decode(resp.encoding or "utf-8", "replace")
        target = folder / f"{index:02d}{ext}"
        size = 0
        with target.open("wb") as f:
            async for chunk in resp.aiter_bytes(1 << 16):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise TooBig()
                f.write(chunk)
        return target


async def download_with_fallback(path: str, folder: Path) -> list[Path]:
    if not FALLBACK_HOST:
        return []
    allow_images = path.startswith(("/p/", "/share/p/"))  # у рилсов картинка — это просто превью
    async with httpx.AsyncClient(
        headers={"User-Agent": BOT_UA}, follow_redirects=True, timeout=60
    ) as client:
        first = await _save_stream(client, f"https://{FALLBACK_HOST}{path}", folder, 1)
        if isinstance(first, Path):
            if first.suffix in IMAGE_EXT and not allow_images:
                first.unlink()
                return []
            return [first]
        og = parse_og(first)
        urls = og["video"] or (og["image"] if allow_images else [])
        for i, media_url in enumerate(urls[:MAX_ITEMS], start=1):
            saved = await _save_stream(client, media_url, folder, i)
            if not isinstance(saved, Path):
                log.warning("fallback: по адресу %s не медиафайл", media_url)
    return collect_files(folder)


async def download_media(url: str, path: str, folder: Path) -> list[Path]:
    try:
        files = await asyncio.to_thread(download_with_ytdlp, url, folder)
        if files:
            return files
    except Exception as e:  # yt-dlp не смог (нужен логин, это фото и т.п.)
        log.warning("yt-dlp не справился с %s: %s", url, e)
    for leftover in folder.iterdir():
        leftover.unlink()
    return await download_with_fallback(path, folder)


# ---------- telegram ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет!\n\n"
        "Пришли мне ссылку на рилс или пост из инстаграма — я скачаю его и пришлю "
        "видео или фото прямо сюда.\n"
        "Можно писать мне в личку, а можно добавить в групповой чат.\n\n"
        "Пример ссылки: https://www.instagram.com/reel/xxxxxxx"
    )


async def send_file(message, file: Path):
    is_video = file.suffix.lower() in VIDEO_EXT
    timeouts = dict(read_timeout=300, write_timeout=300, connect_timeout=30)
    with file.open("rb") as f:
        if SEND_AS_DOCUMENT:
            name = f"instagram{file.suffix.lower()}"
            await message.reply_document(
                f, filename=name, disable_content_type_detection=True, **timeouts
            )
        elif is_video:
            await message.reply_video(f, supports_streaming=True, **timeouts)
        else:
            await message.reply_photo(f, **timeouts)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    text = message.text or message.caption
    if not text:
        return

    for url, path in find_links(text):
        action = ChatAction.UPLOAD_DOCUMENT if SEND_AS_DOCUMENT else ChatAction.UPLOAD_VIDEO
        try:
            await message.chat.send_action(action)
        except Exception:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            try:
                files = await download_media(url, path, folder)
            except TooBig:
                await message.reply_text("Файл больше 50 МБ — телеграм не даёт боту такое отправить 😔")
                continue
            except Exception:
                log.exception("не удалось скачать %s", url)
                files = []

            if not files:
                await message.reply_text(
                    "Не получилось скачать 😔 Возможно, аккаунт закрытый, пост удалён "
                    "или инстаграм временно не отдаёт видео. Попробуй ещё раз чуть позже."
                )
                continue

            for file in files:
                if file.stat().st_size > MAX_BYTES:
                    await message.reply_text(
                        "Файл больше 50 МБ — телеграм не даёт боту такое отправить 😔"
                    )
                    continue
                try:
                    await send_file(message, file)
                except Exception:
                    log.exception("не удалось отправить %s", file.name)
                    await message.reply_text("Скачал, но не смог отправить файл 😔 Попробуй ещё раз.")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

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

    app = ApplicationBuilder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message)
    )

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
