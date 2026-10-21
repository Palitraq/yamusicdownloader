import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import FileCache, MusicBot, Settings, build_application


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = MusicBot(
            Settings("123:fixture", "fixture", "ffmpeg", workers=1, queue_size=2)
        )

    def update(self, user):
        message = SimpleNamespace(
            text="https://music.yandex.ru/track/1",
            caption=None,
            entities=(),
            caption_entities=(),
            reply_text=AsyncMock(return_value=SimpleNamespace()),
        )
        return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=user))

    async def test_accepts_different_users_and_blocks_duplicate_pending(self):
        first, second = self.update(10), self.update(20)
        await self.service.receive(first, None)
        await self.service.receive(second, None)
        self.assertEqual(self.service.queue.qsize(), 2)
        self.assertEqual(self.service.pending, {10, 20})
        await self.service.receive(first, None)
        self.assertEqual(self.service.queue.qsize(), 2)
        first.effective_message.reply_text.assert_awaited_with(
            "Предыдущий трек ещё обрабатывается."
        )

    async def test_queue_is_bounded(self):
        for uid in (1, 2, 3):
            await self.service.receive(self.update(uid), None)
        self.assertEqual(self.service.queue.qsize(), 2)
        self.assertNotIn(3, self.service.pending)

    async def test_worker_releases_user_after_download_failure(self):
        update = self.update(7)
        status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        update.effective_message.reply_text.return_value = status
        self.service.deliver = AsyncMock(side_effect=RuntimeError("fixture"))
        await self.service.receive(update, None)
        worker = asyncio.create_task(self.service.worker(SimpleNamespace()))
        with self.assertLogs("music_bot", level="ERROR"):
            await asyncio.wait_for(self.service.queue.join(), 2)
        self.assertNotIn(7, self.service.pending)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        status.edit_text.assert_awaited_with("Не получилось скачать. Попробуй позже.")


class CacheTests(unittest.TestCase):
    def test_scope_and_invalidation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cache.db"
            cache = FileCache(path, "first-token", 60)
            cache.put("1", "audio", "file-id", "caption")
            self.assertEqual(cache.get("1"), ("audio", "file-id", "caption"))
            other = FileCache(path, "second-token", 60)
            self.assertIsNone(other.get("1"))
            other.close()
            cache.delete("1")
            self.assertIsNone(cache.get("1"))
            cache.close()

    def test_application_builds_without_network_or_allowlist(self):
        application = build_application(Settings("123456:" + "A" * 35, "fixture", "ffmpeg"))
        self.assertIsNotNone(application.post_stop)
        self.assertEqual(len(application.handlers[0]), 2)
