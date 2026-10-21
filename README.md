# Yandex Music Telegram Bot

Бот скачивает треки по ссылкам Яндекс Музыки и отправляет их в Telegram. Принимает сообщения от любых пользователей; запросы к Яндексу идут через аккаунт владельца.

Нужны Python 3.10+, FFmpeg, токен Telegram-бота и OAuth-токен Яндекс Музыки.

## Запуск

На Windows запусти `setup.bat`, заполни `.env`, затем запусти `run.bat`.

На Linux и macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Заполни токены в .env.
python bot.py
```

FFmpeg должен быть в `PATH`. Если он установлен в другом месте, укажи путь к исполняемому файлу в `FFMPEG_PATH`.

### Токены

Telegram-токен выдаёт [@BotFather](https://t.me/BotFather) после `/newbot`.

В YandexMusicModClient токен текущего входа можно прочитать через DevTools → Console:

```js
JSON.parse(localStorage.getItem("oauth") || "null")?.value
```

DevTools открываются сочетанием `Ctrl+Shift+I`, если они включены в моде. В обычной веб-версии структура хранилища может отличаться.

Запиши токен в `YANDEX_MUSIC_TOKEN` без префикса `OAuth`. Пароль аккаунта не нужен. После изменения настроек перезапусти бота. Не публикуй `.env`: токен даёт доступ к аккаунту.

## Использование

Отправь боту ссылку вида:

```text
https://music.yandex.ru/album/123/track/456
https://music.yandex.ru/track/456
```

Поддерживаются домены `music.yandex.ru`, `.com`, `.kz`, `.by` и `.uz`. Альбомы, плейлисты и короткие ссылки без ID трека не поддерживаются.

Бот отвечает «Скачиваю…», затем присылает файл с названием и качеством. Промежуточное сообщение удаляется после отправки.

Сначала запрашивается FLAC. Если Яндекс возвращает AAC или MP3, сохраняется доступный формат. Аудио не перекодируется; FFmpeg добавляет теги и обложку. FLAC отправляется документом, MP3 и M4A — аудиозаписью.

## Настройки

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Токен бота |
| `YANDEX_MUSIC_TOKEN` | — | OAuth-токен владельца |
| `FFMPEG_PATH` | поиск в `PATH` | Путь к FFmpeg |
| `WORKERS` | `2` | Параллельные скачивания |
| `QUEUE_SIZE` | `50` | Размер очереди |
| `USER_COOLDOWN_SECONDS` | `5` | Пауза между запросами одного пользователя |
| `CACHE_HOURS` | `6` | Срок хранения Telegram `file_id` |
| `MAX_FILE_MB` | `48` | Максимальный размер файла |
| `TELEGRAM_API_ROOT` | пусто | Адрес собственного Bot API server |

У каждого пользователя может быть один незавершённый запрос. Очередь хранится в памяти и теряется при перезапуске. Кеш — в `data/cache.sqlite3`; временные аудиофайлы удаляются после обработки.

Обычный Bot API принимает файлы до 50 МБ. Для крупных FLAC нужен [локальный Bot API server](https://core.telegram.org/bots/api#using-a-local-bot-api-server): укажи его корневой адрес в `TELEGRAM_API_ROOT` и увеличь `MAX_FILE_MB` (до 1900).

Доступность треков и качества зависит от аккаунта и каталога Яндекса. Бот не снимает ограничения подписки. Загрузчик использует внутренний API, который может меняться.

## Проверка

```sh
python -m unittest discover -s tests -v
```

Тесты работают без токенов и сети. Для теста обработки аудио нужен FFmpeg.

`python bot.py --check` проверяет настройки и наличие FFmpeg без подключения к Telegram.

## Лицензия

MIT. Подпись запросов, обработка AES-CTR и подготовка аудиофайлов адаптированы из [YandexMusicModClient](https://github.com/TheKing-OfTime/YandexMusicModClient), © 2024 TheKingOfTime. Исходное уведомление об авторских правах сохранено в [LICENSE](LICENSE).
