from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from telegram import InputFile, Message, Update
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from music import MusicClient, MusicError, track_id_from_url

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("music_bot")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    music_token: str
    ffmpeg: str
    workers: int = 2
    queue_size: int = 50
    cooldown: int = 5
    cache_hours: int = 6
    max_bytes: int = 48 * 1024 * 1024
    api_root: str = ""

    @classmethod
    def load(cls) -> Settings:
        load_dotenv(ROOT / ".env")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        music_token = os.getenv("YANDEX_MUSIC_TOKEN", "").strip().removeprefix("OAuth ").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", bot_token):
            raise ValueError("Укажи TELEGRAM_BOT_TOKEN в файле .env.")
        if not music_token or music_token.startswith("токен"):
            raise ValueError("Укажи YANDEX_MUSIC_TOKEN в файле .env.")
        candidates = [os.getenv("FFMPEG_PATH", ""), shutil.which("ffmpeg")]
        ffmpeg = next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), "")
        if not ffmpeg:
            raise ValueError("Не найден FFmpeg. Укажи FFMPEG_PATH в файле .env.")

        def number(name: str, default: int, low: int, high: int) -> int:
            try:
                result = int(os.getenv(name, str(default)))
            except ValueError:
                raise ValueError(f"{name}: нужно целое число.") from None
            if not low <= result <= high:
                raise ValueError(f"{name}: допустимо от {low} до {high}.")
            return result

        api_root = os.getenv("TELEGRAM_API_ROOT", "").strip().rstrip("/")
        if api_root and not api_root.startswith(("http://", "https://")):
            raise ValueError("TELEGRAM_API_ROOT должен начинаться с http:// или https://.")
        return cls(
            bot_token,
            music_token,
            ffmpeg,
            number("WORKERS", 2, 1, 8),
            number("QUEUE_SIZE", 50, 1, 500),
            number("USER_COOLDOWN_SECONDS", 5, 0, 3600),
            number("CACHE_HOURS", 6, 1, 168),
            number("MAX_FILE_MB", 48, 1, 1900 if api_root else 48) * 1024 * 1024,
            api_root,
        )


class FileCache:
    def __init__(self, path: Path, scope: str, ttl: int):
        self.scope, self.ttl = scope, ttl
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS files (scope TEXT, track TEXT, kind TEXT, file_id TEXT, caption TEXT, expires REAL, PRIMARY KEY(scope, track))"
        )
        self.db.execute("DELETE FROM files WHERE expires < ?", (time.time(),))
        self.db.commit()

    def get(self, track: str):
        return self.db.execute(
            "SELECT kind, file_id, caption FROM files WHERE scope=? AND track=? AND expires>?",
            (self.scope, track, time.time()),
        ).fetchone()

    def put(self, track: str, kind: str, file_id: str, caption: str):
        self.db.execute("DELETE FROM files WHERE expires < ?", (time.time(),))
        self.db.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
            (self.scope, track, kind, file_id, caption, time.time() + self.ttl),
        )
        self.db.commit()

    def delete(self, track: str):
        self.db.execute("DELETE FROM files WHERE scope=? AND track=?", (self.scope, track))
        self.db.commit()

    def close(self):
        self.db.close()


@dataclass
class Job:
    user_id: int
    track_id: str
    message: Message
    status: Message


class MusicBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue: asyncio.Queue[Job] = asyncio.Queue(settings.queue_size)
        self.pending: set[int] = set()
        self.last_request: dict[int, float] = {}
        self.tasks: list[asyncio.Task] = []
        self.music: MusicClient | None = None
        self.cache: FileCache | None = None
        self.data = ROOT / "data"
        self.temp = self.data / "temp"

    async def start(self, application: Application):
        self.temp.mkdir(parents=True, exist_ok=True)
        self.music = MusicClient(
            self.settings.music_token, self.settings.ffmpeg, self.settings.max_bytes
        )
        scope = hashlib.sha256(
            (self.settings.bot_token + ":" + self.settings.music_token).encode()
        ).hexdigest()
        self.cache = FileCache(self.data / "cache.sqlite3", scope, self.settings.cache_hours * 3600)
        self.tasks = [
            asyncio.create_task(self.worker(application), name=f"download-{i}")
            for i in range(self.settings.workers)
        ]
        LOG.info("Bot started: %s download workers; public access enabled", self.settings.workers)

    async def stop(self, _application: Application):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def close(self, _application: Application):
        if self.music:
            await self.music.close()
        if self.cache:
            self.cache.close()

    async def welcome(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        if update.effective_message:
            await update.effective_message.reply_text("Пришли ссылку на трек Яндекс Музыки.")

    async def receive(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        message, user = update.effective_message, update.effective_user
        if not message or not user:
            return
        text = message.text or message.caption or ""
        urls = re.findall(r"https?://[^\s<>]+", text)
        urls += [
            entity.url for entity in (*message.entities, *message.caption_entities) if entity.url
        ]
        valid = []
        for url in urls:
            with contextlib.suppress(MusicError):
                valid.append(track_id_from_url(url))
        if not valid:
            await message.reply_text("Пришли ссылку на один трек Яндекс Музыки.")
            return
        if len(set(valid)) != 1:
            await message.reply_text("По одному треку за раз.")
            return
        if user.id in self.pending:
            await message.reply_text("Предыдущий трек ещё обрабатывается.")
            return
        now = time.monotonic()
        if now - self.last_request.get(user.id, float("-inf")) < self.settings.cooldown:
            await message.reply_text("Подожди несколько секунд.")
            return
        if self.queue.full():
            await message.reply_text("Сейчас много запросов. Попробуй чуть позже.")
            return
        status = await message.reply_text(
            "Скачиваю…"
            if self.queue.empty() and len(self.pending) < self.settings.workers
            else "В очереди…"
        )
        self.pending.add(user.id)
        self.last_request[user.id] = now
        if len(self.last_request) > 10000:
            self.last_request = {
                uid: when
                for uid, when in self.last_request.items()
                if now - when < max(60, self.settings.cooldown)
            }
        self.queue.put_nowait(Job(user.id, valid[0], message, status))

    async def cached_send(self, application: Application, job: Job, cached):
        kind, file_id, caption = cached
        if kind == "audio":
            return await application.bot.send_audio(
                job.message.chat_id, audio=file_id, caption=caption
            )
        return await application.bot.send_document(
            job.message.chat_id, document=file_id, caption=caption
        )

    async def deliver(self, application: Application, job: Job):
        assert self.music is not None and self.cache is not None
        cached = self.cache.get(job.track_id)
        if cached:
            try:
                await self.cached_send(application, job, cached)
                return
            except BadRequest:
                self.cache.delete(job.track_id)
        with tempfile.TemporaryDirectory(prefix="track-", dir=self.temp) as directory:
            audio = await self.music.download(job.track_id, Path(directory))
            with contextlib.suppress(TelegramError):
                await job.status.edit_text("Отправляю…")
            with audio.path.open("rb") as handle:
                upload = InputFile(handle, filename=audio.path.name, read_file_handle=False)
                if audio.path.suffix == ".flac":
                    sent = await application.bot.send_document(
                        job.message.chat_id,
                        document=upload,
                        caption=audio.caption,
                        write_timeout=300,
                        read_timeout=300,
                    )
                    self.cache.put(job.track_id, "document", sent.document.file_id, audio.caption)
                else:
                    sent = await application.bot.send_audio(
                        job.message.chat_id,
                        audio=upload,
                        caption=audio.caption,
                        title=audio.title[:128],
                        performer=audio.artist[:128],
                        duration=audio.duration,
                        write_timeout=300,
                        read_timeout=300,
                    )
                    self.cache.put(job.track_id, "audio", sent.audio.file_id, audio.caption)

    async def worker(self, application: Application):
        while True:
            job = await self.queue.get()
            try:
                with contextlib.suppress(TelegramError):
                    await job.status.edit_text("Скачиваю…")
                for attempt in range(2):
                    try:
                        await asyncio.wait_for(self.deliver(application, job), timeout=900)
                        break
                    except RetryAfter as error:
                        delay = (
                            error.retry_after.total_seconds()
                            if hasattr(error.retry_after, "total_seconds")
                            else float(error.retry_after)
                        )
                        if attempt or delay > 30:
                            raise MusicError(
                                "Telegram просит подождать. Попробуй чуть позже."
                            ) from None
                        await asyncio.sleep(delay + 1)
                with contextlib.suppress(TelegramError):
                    await job.status.delete()
            except MusicError as error:
                with contextlib.suppress(TelegramError):
                    await job.status.edit_text(str(error))
            except asyncio.CancelledError:
                with contextlib.suppress(TelegramError):
                    await job.status.edit_text(
                        "Бот перезапускается. Пришли ссылку ещё раз чуть позже."
                    )
                raise
            except Exception as error:
                # HTTP exception messages may contain tokens in request URLs.
                LOG.error("Download failed: %s", type(error).__name__)
                with contextlib.suppress(TelegramError):
                    await job.status.edit_text("Не получилось скачать. Попробуй позже.")
            finally:
                self.pending.discard(job.user_id)
                self.queue.task_done()


async def handler_error(_update: object, context: ContextTypes.DEFAULT_TYPE):
    LOG.error("Telegram handler failed: %s", type(context.error).__name__)


def build_application(settings: Settings) -> Application:
    service = MusicBot(settings)
    builder = (
        Application.builder()
        .token(settings.bot_token)
        .connection_pool_size(settings.workers + 8)
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(300)
        .pool_timeout(30)
        .post_init(service.start)
        .post_stop(service.stop)
        .post_shutdown(service.close)
    )
    if settings.api_root:
        builder = (
            builder.base_url(settings.api_root + "/bot")
            .base_file_url(settings.api_root + "/file/bot")
            .local_mode(True)
        )
    application = builder.build()
    application.add_handler(CommandHandler(["start", "help"], service.welcome))
    application.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, service.receive)
    )
    application.add_error_handler(handler_error)
    return application


def main():
    parser = argparse.ArgumentParser(description="Публичный Telegram-бот для ссылок Яндекс Музыки")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Проверить настройки и FFmpeg без подключения к Telegram",
    )
    options = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = Settings.load()
    except ValueError as error:
        parser.exit(2, str(error) + "\n")
    if options.check:
        print("Настройки заполнены. FFmpeg найден. Подключения к Telegram не было.")
        return
    build_application(settings).run_polling(allowed_updates=["message"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
