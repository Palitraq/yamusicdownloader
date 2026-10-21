"""Yandex Music API client and audio file preparation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class MusicError(Exception):
    """An error message suitable for a Telegram reply."""


class ApiError(MusicError):
    def __init__(self, status: int):
        self.status = status
        super().__init__("Яндекс Музыка временно недоступна. Попробуй позже.")


MUSIC_HOSTS = {f"music.yandex.{tld}" for tld in ("ru", "com", "kz", "by", "uz")}
CDN_DOMAINS = (
    "yandex.net",
    "yandex.ru",
    "yandex.com",
    "yandex.kz",
    "yandex.by",
    "yandex.uz",
    "yandexcloud.net",
)
CODECS = ("flac", "aac", "he-aac", "mp3", "flac-mp4", "aac-mp4", "he-aac-mp4")
EXTENSIONS = {
    "flac": "flac",
    "flac-mp4": "flac",
    "aac": "m4a",
    "he-aac": "m4a",
    "aac-mp4": "m4a",
    "he-aac-mp4": "m4a",
    "mp3": "mp3",
}
SIGNING_KEY = b"kzqU4XhfCaY6B6JTHODeq5"  # Shared desktop client signing key.


def track_id_from_url(value: str) -> str:
    try:
        url = urlsplit(value.strip().rstrip(".,!);>"))
        if (
            url.scheme != "https"
            or url.hostname not in MUSIC_HOSTS
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            raise ValueError
        match = re.fullmatch(r"/(?:album/\d+/)?track/(\d{1,20})/?", url.path)
        if not match:
            raise ValueError
        return match.group(1)
    except ValueError:
        raise MusicError("Пришли ссылку на один трек Яндекс Музыки.") from None


def safe_filename(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip(" .")[:160].rstrip(" .")
    if not name or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name):
        name = "track_" + name
    return name


def validate_media_url(value: str) -> None:
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
            raise ValueError
        if not any(host == domain or host.endswith("." + domain) for domain in CDN_DOMAINS):
            raise ValueError
    except ValueError:
        raise MusicError("Яндекс вернул неподдерживаемую ссылку на файл.") from None


def sign_file_info(
    track_id: str,
    codecs: tuple[str, ...],
    timestamp: int,
    quality: str = "lossless",
    transport: str = "encraw",
) -> str:
    data = f"{timestamp}{track_id}{quality}{''.join(codecs)}{transport}"
    return (
        base64.b64encode(hmac.new(SIGNING_KEY, data.encode(), hashlib.sha256).digest())
        .decode()
        .rstrip("=")
    )


@dataclass(frozen=True)
class Download:
    path: Path
    title: str
    artist: str
    duration: int
    codec: str
    bitrate: int

    @property
    def quality_label(self) -> str:
        if self.codec.startswith("flac"):
            return "FLAC · без потерь"
        codec = "MP3" if self.codec == "mp3" else "AAC"
        return f"{codec} · {self.bitrate} кбит/с" if self.bitrate else codec

    @property
    def caption(self) -> str:
        return f"{self.artist} — {self.title}\n{self.quality_label}"[:1000]


class MusicClient:
    def __init__(self, token: str, ffmpeg: str, max_bytes: int):
        self.ffmpeg = ffmpeg
        self.max_bytes = max_bytes
        self.api = httpx.AsyncClient(
            base_url="https://api.music.yandex.net/",
            timeout=httpx.Timeout(30, connect=10),
            headers={
                "Authorization": f"OAuth {token}",
                "X-Yandex-Music-Client": "YandexMusicDesktopAppWindows/5.123.4",
                "X-Yandex-Music-Without-Invocation-Info": "1",
                "Accept-Language": "ru",
            },
        )
        # CDN requests must not receive the account's Authorization header.
        self.media = httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=15), follow_redirects=False
        )

    async def close(self) -> None:
        await self.api.aclose()
        await self.media.aclose()

    async def request(self, route: str, params: dict | None = None, form: dict | None = None):
        try:
            response = (
                await self.api.post(route, data=form)
                if form is not None
                else await self.api.get(route, params=params)
            )
            if response.status_code >= 300:
                raise ApiError(response.status_code)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise MusicError("Яндекс Музыка не ответила. Попробуй позже.") from None
        if isinstance(data, dict) and "error" in data:
            raise MusicError("Трек недоступен для этого аккаунта.")
        return data.get("result", data) if isinstance(data, dict) else data

    async def file_info(self, track_id: str) -> dict:
        for codecs in (("flac", "flac-mp4"), CODECS):
            timestamp = int(time.time())
            try:
                data = await self.request(
                    "get-file-info",
                    {
                        "ts": timestamp,
                        "trackId": track_id,
                        "quality": "lossless",
                        "codecs": ",".join(codecs),
                        "transports": "encraw",
                        "sign": sign_file_info(track_id, codecs, timestamp),
                    },
                )
            except ApiError as error:
                if codecs != CODECS and error.status in (400, 404, 406, 422):
                    continue
                raise
            info = data.get("downloadInfo", {}) if isinstance(data, dict) else {}
            if info.get("url") and (codecs == CODECS or info.get("codec") in ("flac", "flac-mp4")):
                if info.get("codec") not in EXTENSIONS:
                    raise MusicError("Яндекс вернул неподдерживаемый формат.")
                return info
        raise MusicError("Не удалось получить файл этого трека.")

    async def save_media(
        self, url: str, destination: Path, max_bytes: int, key_hex: str | None = None
    ) -> None:
        decryptor = None
        if key_hex is not None:
            try:
                decryptor = Cipher(
                    algorithms.AES(bytes.fromhex(key_hex)), modes.CTR(bytes(16))
                ).decryptor()
            except (ValueError, TypeError):
                raise MusicError("Не удалось обработать файл Яндекса.") from None
        try:
            for _ in range(6):
                validate_media_url(url)
                async with self.media.stream("GET", url) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers.get("location", "")))
                        continue
                    response.raise_for_status()
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise MusicError("Файл слишком большой для отправки этим ботом.")
                    written = 0
                    with destination.open("wb") as output:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            written += len(chunk)
                            if written > max_bytes:
                                raise MusicError("Файл слишком большой для отправки этим ботом.")
                            output.write(decryptor.update(chunk) if decryptor else chunk)
                        if decryptor:
                            output.write(decryptor.finalize())
                    if not written:
                        raise MusicError("Яндекс вернул пустой файл.")
                    return
            raise MusicError("Не удалось открыть файл Яндекса.")
        except httpx.HTTPError:
            raise MusicError("Скачивание прервалось. Попробуй ещё раз.") from None

    async def download(self, track_id: str, directory: Path) -> Download:
        info, tracks = await asyncio.gather(
            self.file_info(track_id),
            self.request(
                "tracks",
                form={"trackIds": track_id, "removeDuplicates": "false", "withProgress": "false"},
            ),
        )
        if not isinstance(tracks, list) or not tracks:
            raise MusicError("Трек не найден.")
        track = tracks[0]
        title = str(track.get("title") or "Без названия")
        if track.get("version"):
            title += f" ({track['version']})"
        artist = (
            ", ".join(str(item.get("name", "")) for item in track.get("artists", []))
            or "Неизвестный исполнитель"
        )
        extension = EXTENSIONS[info["codec"]]
        source = directory / ("source." + info["codec"])
        transport = info.get("transport")
        if transport not in ("raw", "encraw"):
            raise MusicError("Яндекс вернул неподдерживаемый способ доставки.")
        if transport == "encraw" and not info.get("key"):
            raise MusicError("Яндекс не вернул данные для обработки файла.")
        await self.save_media(
            info["url"], source, self.max_bytes, info.get("key") if transport == "encraw" else None
        )
        cover = directory / "cover.jpg"
        if track.get("coverUri"):
            try:
                await self.save_media(
                    "https://"
                    + track["coverUri"].removeprefix("https://").replace("%%", "400x400"),
                    cover,
                    5 * 1024 * 1024,
                )
            except MusicError:
                cover.unlink(missing_ok=True)
        output = directory / (safe_filename(f"{artist} — {title}") + "." + extension)
        album = (track.get("albums") or [{}])[0]
        args = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
        ]
        if cover.exists():
            args += ["-i", str(cover)]
        args += ["-map", "0:a:0", "-c:a", "copy"]
        if cover.exists():
            args += ["-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
        for name, value in {
            "title": title,
            "artist": artist,
            "album": album.get("title"),
            "date": album.get("year"),
        }.items():
            if value is not None:
                args += ["-metadata", f"{name}={value}"]
        if extension == "mp3":
            args += ["-id3v2_version", "3"]
        args.append(str(output))
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            await asyncio.wait_for(process.communicate(), timeout=120)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode or not output.exists() or not output.stat().st_size:
            raise MusicError("Не удалось подготовить аудиофайл.")
        if output.stat().st_size > self.max_bytes:
            raise MusicError("Файл слишком большой для отправки этим ботом.")
        return Download(
            output,
            title,
            artist,
            int(track.get("durationMs", 0) / 1000),
            info["codec"],
            int(info.get("bitrate") or 0),
        )
