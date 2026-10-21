import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from music import (
    ApiError,
    MusicClient,
    MusicError,
    safe_filename,
    sign_file_info,
    track_id_from_url,
    validate_media_url,
)

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = Path(os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or "__missing_ffmpeg__")


class ParsingTests(unittest.TestCase):
    def test_links(self):
        self.assertEqual(
            track_id_from_url(
                "https://music.yandex.ru/album/43723917/track/155229454?utm_source=share"
            ),
            "155229454",
        )
        self.assertEqual(track_id_from_url("https://music.yandex.com/track/123/"), "123")

    def test_reject_untrusted_links(self):
        for link in (
            "http://music.yandex.ru/track/1",
            "https://music.yandex.ru.evil.com/track/1",
            "https://evil.com/track/1",
            "https://user@music.yandex.ru/track/1",
            "https://music.yandex.ru:123/track/1",
            "https://music.yandex.ru/album/1",
            "https://127.0.0.1/track/1",
        ):
            with self.subTest(link=link), self.assertRaises(MusicError):
                track_id_from_url(link)

    def test_media_url_allowlist(self):
        validate_media_url("https://strm-rad-01.strm.yandex.net/music/file")
        for link in (
            "https://evil.com/audio",
            "http://strm.yandex.net/file",
            "https://yandex.net.evil.com/file",
            "https://127.0.0.1/audio",
            "https://x@yandex.net/audio",
        ):
            with self.subTest(link=link), self.assertRaises(MusicError):
                validate_media_url(link)

    def test_sign_matches_original_node_implementation(self):
        self.assertEqual(
            sign_file_info("155229454", ("flac", "flac-mp4"), 1700000000),
            "Gtudc82lP86eD3D6F1X/2gUFwvb0R0idHI6nLXE/7Tc",
        )

    def test_filename(self):
        self.assertEqual(safe_filename("../a:b*?"), "_a_b__")
        self.assertEqual(safe_filename("CON"), "track_CON")
        self.assertLessEqual(len(safe_filename("x" * 1000)), 160)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = MusicClient("test-secret", "ffmpeg", 100000)

    async def asyncTearDown(self):
        await self.client.close()

    async def test_prefers_flac_and_keeps_fallback_honest(self):
        calls = []

        def respond(request):
            calls.append(request.url.params["codecs"])
            info = {"url": "https://strm.yandex.net/file", "codec": "aac-mp4", "bitrate": 192}
            return httpx.Response(200, json={"downloadInfo": info})

        await self.client.api.aclose()
        self.client.api = httpx.AsyncClient(
            base_url="https://api.music.yandex.net/", transport=httpx.MockTransport(respond)
        )
        result = await self.client.file_info("1")
        self.assertEqual(result["codec"], "aac-mp4")
        self.assertEqual(calls[0], "flac,flac-mp4")
        self.assertEqual(len(calls), 2)

    async def test_flac_needs_one_request(self):
        self.client.request = AsyncMock(
            return_value={
                "downloadInfo": {"url": "https://strm.yandex.net/file", "codec": "flac-mp4"}
            }
        )
        self.assertEqual((await self.client.file_info("1"))["codec"], "flac-mp4")
        self.assertEqual(self.client.request.await_count, 1)

    async def test_auth_failure_not_hidden_by_fallback(self):
        self.client.request = AsyncMock(side_effect=ApiError(401))
        with self.assertRaises(ApiError):
            await self.client.file_info("1")
        self.assertEqual(self.client.request.await_count, 1)

    async def test_decryption_across_unaligned_chunks_without_auth_on_cdn(self):
        clear = bytes(range(256)) * 513 + b"tail!"
        key = bytes(range(16))
        encryptor = Cipher(algorithms.AES(key), modes.CTR(bytes(16))).encryptor()
        encrypted = encryptor.update(clear) + encryptor.finalize()

        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                for start in range(0, len(encrypted), 113):
                    yield encrypted[start : start + 113]

        def respond(request):
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, stream=Chunks())

        await self.client.media.aclose()
        self.client.media = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "audio"
            await self.client.save_media("https://strm.yandex.net/file", output, 200000, key.hex())
            self.assertEqual(output.read_bytes(), clear)

    async def test_download_size_is_enforced_without_content_length(self):
        await self.client.media.aclose()
        self.client.media = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 100))
        )
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(MusicError):
            await self.client.save_media("https://strm.yandex.net/file", Path(folder) / "audio", 50)

    async def test_redirect_to_external_host_is_rejected(self):
        await self.client.media.aclose()
        self.client.media = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(302, headers={"Location": "https://evil.com/file"})
            )
        )
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(MusicError):
            await self.client.save_media(
                "https://strm.yandex.net/file", Path(folder) / "audio", 1000
            )


@unittest.skipUnless(FFMPEG.is_file(), "FFmpeg is not installed")
class RemuxTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ffmpeg_preserves_audio_formats_and_adds_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cover_fixture = root / "cover.jpg"
            process = await asyncio.create_subprocess_exec(
                str(FFMPEG),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=16x16",
                "-frames:v",
                "1",
                "-y",
                str(cover_fixture),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            self.assertEqual(process.returncode, 0)
            for codec, encoder, extension, magic in (
                ("flac-mp4", "flac", "flac", b"fLaC"),
                ("aac-mp4", "aac", "m4a", b"ftyp"),
                ("mp3", "libmp3lame", "mp3", b"ID3"),
            ):
                with self.subTest(codec=codec):
                    fixture = root / ("fixture." + extension)
                    command = [
                        str(FFMPEG),
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:duration=0.15",
                        "-c:a",
                        encoder,
                        "-y",
                        str(fixture),
                    ]
                    process = await asyncio.create_subprocess_exec(
                        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    await process.communicate()
                    self.assertEqual(process.returncode, 0)
                    directory = root / extension
                    directory.mkdir()
                    client = MusicClient("fixture", str(FFMPEG), 1000000)
                    client.file_info = AsyncMock(
                        return_value={
                            "codec": codec,
                            "url": "https://strm.yandex.net/file",
                            "transport": "raw",
                            "bitrate": 192,
                        }
                    )
                    client.request = AsyncMock(
                        return_value=[
                            {
                                "title": "Fixture",
                                "artists": [{"name": "Test Artist"}],
                                "durationMs": 150,
                                "coverUri": "avatars.yandex.net/fixture/%%",
                            }
                        ]
                    )

                    async def save(_url, destination, _limit, _key=None, _fixture=fixture):
                        shutil.copyfile(
                            cover_fixture if destination.name == "cover.jpg" else _fixture,
                            destination,
                        )

                    client.save_media = save
                    try:
                        result = await client.download("1", directory)
                        data = result.path.read_bytes()
                        self.assertIn(magic, data[:16])
                        self.assertEqual(result.path.suffix, "." + extension)
                        probe = await asyncio.create_subprocess_exec(
                            str(FFMPEG),
                            "-i",
                            str(result.path),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        _, details = await probe.communicate()
                        self.assertIn(b"Test Artist", details)
                        self.assertIn(b"Fixture", details)
                        self.assertIn(b"attached pic", details)
                        client.request.assert_awaited_once_with(
                            "tracks",
                            form={
                                "trackIds": "1",
                                "removeDuplicates": "false",
                                "withProgress": "false",
                            },
                        )
                    finally:
                        await client.close()
