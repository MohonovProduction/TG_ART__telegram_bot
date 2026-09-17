# TG ART Bot

A Telegram bot that creates custom-emoji art grids and ordinary static or video sticker packs.

Static images are converted to `100×100` PNG files. Videos are converted to `100×100` WebM/VP9 files with transparency, a maximum frame rate of 30 FPS, a maximum duration of 3 seconds, and no audio.

## Features

- Supports static images and animated videos
- Splits source media into a configurable grid of up to 200 tiles
- Preserves transparency in supported video formats
- Optimizes video emoji files to meet Telegram size limits
- Generates a ZIP archive with all rendered tiles
- Creates and fills a Telegram custom emoji pack automatically
- Creates ordinary static and video sticker packs from a batch of files
- Lets you assign one emoji to all stickers or an individual emoji to each sticker
- Accepts a local folder path for batch sticker uploads
- Restricts access through a persistent allowlist with owner, administrator, and user roles
- Supports a 12×12 reply keyboard for selecting and confirming grids
- Suggests transliterated link names and alternatives when a name is taken
- Accepts Telegram uploads or local file paths
- Converts videos to Telegram video notes with sound and a centered square crop
- Exports static stickers and custom emoji to transparent PNG in batches
- Exports video and TGS stickers/custom emoji to alpha-channel ProRes 4444 MOV
- Saves only finished PNG/MOV files to the default download folder or a custom path

## Requirements

- macOS or Linux
- Python 3.9 or newer
- FFmpeg with `libvpx-vp9` support
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

Install FFmpeg on macOS with Homebrew:

```bash
brew install ffmpeg
```

For animated emoji, ProRes 4444 MOV with an alpha channel is recommended. Send videos to the bot as documents; otherwise, Telegram may recompress them and remove transparency.

## Installation

Clone the repository and open its directory:

```bash
git clone <repository-url>
cd TG_ART_bot
```

Create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create the environment configuration:

```bash
cp .env.example .env
```

Open `.env` and provide your bot token and Telegram user ID:

```env
BOT_TOKEN=123456:your-telegram-bot-token
ALLOWED_USER_ID=123456789
```

You can find your Telegram user ID using [@userinfobot](https://t.me/userinfobot).

## Configuration

The following environment variables are available:

| Variable | Default | Description |
| --- | --- | --- |
| `BOT_TOKEN` | Required | Telegram bot token issued by BotFather |
| `ALLOWED_USER_ID` | Required | Owner Telegram ID; other users are managed with `/users` |
| `ACCESS_DB` | `./data/access.sqlite3` | Persistent user and role database |
| `ALLOW_LOCAL_PATHS` | `true` | Enables local filesystem paths for the owner; disabled in Compose |
| `MAX_CONCURRENT_JOBS` | `2` | Maximum simultaneous request handlers |
| `OUTPUT_DIR` | `./output` | Directory for rendered tiles and ZIP archives |
| `TEMP_DIR` | `./temp` | Directory for temporary uploaded files |
| `DOWNLOAD_DIR` | `./downloads` | Default directory for downloaded sticker batches |
| `DEFAULT_FPS` | `30` | Output video frame rate, limited to 30 FPS |
| `DEFAULT_DURATION` | `3` | Output video duration, limited to 3 seconds |
| `MAX_EMOJI_SIZE_KB` | `256` | Maximum size of each rendered video emoji |
| `MAX_STATIC_STICKER_SIZE_KB` | `512` | Maximum size of each converted static sticker |
| `MAX_VIDEO_STICKER_SIZE_KB` | `256` | Maximum size of each converted video sticker |
| `PACK_TITLE_SUFFIX` | `by @mohonovproduction` | Text automatically appended to pack titles |

## Running the bot

Activate the virtual environment and start the bot:

```bash
source .venv/bin/activate
python -m app.bot
```

The bot uses long polling, so the process must remain running while you use it.

### Docker: отдельный бот с новым токеном

Для сборки и запуска контейнера на Mac установите и запустите
[Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/).
Обычный запуск через `run-bot.command` не требует Docker и продолжает использовать
старый токен из `.env`.

Создайте отдельную конфигурацию контейнера:

```bash
cp .env.cloud.example .env.cloud
```

В `.env.cloud` укажите **новый** `BOT_TOKEN` и свой `ALLOWED_USER_ID`.
Затем выполните из каталога проекта:

```bash
docker compose up -d --build
docker compose logs -f --tail=100 bot
```

Остановить контейнер:

```bash
docker compose down
```

Исходники общие, но два бота используют разные токены и разные каталоги данных.
Личный бот и контейнер можно запускать одновременно. Для одного токена должен
работать только один polling-процесс. Токены не входят в образ: Docker копирует
только `app/` и `requirements.txt`; `.env.cloud` передаётся при запуске.

FFmpeg устанавливается в образ автоматически. Контейнер работает от обычного
пользователя, без входящих портов. Файлы сохраняются в Docker volume `bot-data`
под `/data`; `docker compose down` его сохраняет, а `docker compose down -v`
удаляет вместе с файлами. Volume необходимо резервировать отдельно от образа.

Все текущие функции включены. Локальные пути внутри контейнера относятся к его
файловой системе, а не к Mac: отправляйте исходники через Telegram. Экспорт
сохраняется в `/data/downloads` и отправляется в чат в пределах существующего
лимита отправки. Через стандартный Bot API нельзя скачать входящий файл больше
20 MB. Папки Mac автоматически не подключаются.

Владелец задаётся через `ALLOWED_USER_ID`. Пользователи и роли сохраняются
в `/data/access.sqlite3`; локальный бот использует отдельную базу `./data/access.sqlite3`.
После перезапуска незавершённые диалоги и
кнопки предпросмотра сбрасываются: используйте `/start`; созданные паки и файлы
в volume сохраняются.

Для будущей машины Linux amd64 можно собрать отдельный образ:

```bash
docker buildx build --platform linux/amd64 -t tg-art-bot:amd64 --load .
```

Развёртывание в Yandex Cloud на этом этапе не выполняется.

### Локальный запуск на macOS

After installation, open `run-bot.command` with a double click in Finder. It starts
the bot in Terminal and keeps the log window open if it stops.

`TG ART Bot.app` is a Spotlight-friendly launcher. Move it to `/Applications` once:

```bash
cp -R "TG ART Bot.app" /Applications/
```

Then press `Command + Space`, type `TG ART Bot`, and press Enter. The app opens a
Terminal window with the bot. The launcher expects this project to remain at
`/Users/mohonovproduction/Documents/TG_ART_bot`.

When the bot is managed as a macOS background service, its logs are stored in
`~/Library/Logs/TG_ART_bot/`.

### Управление с iPhone

После установки службы macOS бот можно запускать, останавливать и проверять через
действие **«Запустить сценарий по SSH»** в приложении «Команды» на iPhone. Команды
для действий соответственно:

```bash
/Users/mohonovproduction/Documents/TG_ART_bot/macos/tg-art-bot-service start
/Users/mohonovproduction/Documents/TG_ART_bot/macos/tg-art-bot-service stop
/Users/mohonovproduction/Documents/TG_ART_bot/macos/tg-art-bot-service status
```

На Mac предварительно включите «Удалённый вход» в **Системные настройки → Основные
→ Общий доступ**. Для запуска из-за пределов домашней сети настройте защищённую
сеть, например Tailscale; не открывайте SSH-порт в интернет напрямую.

## Usage

1. Send `/start` and choose **TG Art**, **Стикер пак**, or **Кружок из видео**.
2. For **TG Art**, upload an image or an alpha-channel video as a document (or provide a local file path), enter the grid size, pack title, link name, and one emoji.
3. For **Стикер пак**, choose **Статичные** or **Видео**.
4. Send every source file as a document, or provide an absolute path to a folder on the computer running the bot. Send `/done` when the list is complete.
5. Choose one common emoji or assign an emoji to every sticker in sequence, then enter the title and link name.
6. For a video note, send a Telegram video, a video as a document, or an absolute
   local path. The bot center-crops it to a square, preserves sound, and uses the
   first 60 seconds when the source is longer.
7. For batch downloads, choose **Скачать стикеры / эмодзи**, select a local folder,
   send stickers, custom emoji, or pack links, then press **Сохранить**. Finder paths
   wrapped in single or double quotes are accepted.

Static WebP assets are converted to transparent PNG. Video WebM assets are converted
to ProRes 4444 MOV with alpha at their original resolution and frame rate. TGS assets
are rendered only to MOV; their size can be selected as `×1`–`×4`, `512`, `1024`,
`2048`, or a custom value from 64 to 4096 pixels. Repaintable custom emoji support a
preset or custom HEX color, with white (`#FFFFFF`) as the default. Source and service
files are kept under `TEMP_DIR` only while the operation is running and are removed
afterward; the selected folder receives only completed PNG and MOV files.

After rendering, the bot sends a ZIP archive and creates a pack link similar to:

```text
https://t.me/addemoji/example_by_bot
```

Regular sticker packs use a link such as:

```text
https://t.me/addstickers/example_by_bot
```

Use `/cancel` at any point to stop the current operation.

The Telegram command menu also provides `/emoji_pack`, `/sticker_pack`,
`/video_note`, `/download`, and `/cancel`.

## Управление доступом и новые сценарии

Команда `/users` открывает список пользователей и меню управления:

- Владелец добавляет пользователей, назначает администраторов и отзывает доступ.
- Администратор добавляет обычных пользователей и отзывает их доступ; менять других администраторов он не может.
- Пользователь имеет доступ ко всем функциям обработки медиа.

После выбора действия отправьте числовой Telegram ID или контакт, в котором
Telegram передал `user_id`. Контакт только с номером телефона не подходит.
Затем пользователь самостоятельно открывает бота и отправляет `/start`.
Владелец не может быть удалён или понижен. Работа поддерживается в личных чатах.

При создании или выводе TG Art выберите правый нижний угол на клавиатуре под
полем ввода: `7×5` означает 7 столбцов и 5 строк. Прямоугольник от верхнего
левого угла подсвечивается зелёным цветом кнопок (`style="success"`), после чего требуется подтверждение.
Каждая клетка содержит координаты, поскольку reply-кнопки передают текст,
а не отдельные данные нажатия. Размер можно ввести вручную, до 20×20 и 200
ячеек. Кнопка вывода после создания пака также предлагает выбор сетки.
Если число ячеек отличается от количества emoji в паке, элементы повторяются
по кругу или используются первые элементы.

После названия пака бот предлагает имя ссылки с транслитерацией:
`Летний арт` → `letniy_art_by_<bot_username>`. Можно принять кнопку или ввести
своё имя. При занятом имени предлагается вариант с номером. Проверка доступности
не гарантирует, что имя не займут до создания пака: ошибку Telegram бот покажет в чате.

Локальные пути доступны только владельцу при `ALLOW_LOCAL_PATHS=true`.
Остальные пользователи загружают файлы через Telegram; скачанные результаты
сохраняются в отдельных папках по ID и отправляются в чат. Рендеры также
сохраняются в отдельных папках пользователей. Одновременная обработка ограничена
`MAX_CONCURRENT_JOBS`; повторные запросы во время обработки отклоняются с пояснением.

## Supported source formats

Images:

- PNG
- JPEG
- WebP
- TIFF
- BMP
- HEIC
- AVIF

Videos:

- MOV
- MKV
- WebM
- MP4
- M4V
- AVI

TG Art video files must contain an alpha channel. For reliable transparency, use ProRes 4444, FFV1 with alpha, or a compatible lossless source.

Regular video stickers are converted to WebM/VP9 without audio, with a maximum duration of 3 seconds and 30 FPS. The encoder chooses the highest fitting quality for the 256 KB limit; only if necessary does it reduce FPS and then the visible animation size within the 512×512 canvas. Static stickers are converted to WebP. Both formats use a 512×512 canvas.

## Telegram limitations

- A custom emoji pack can contain no more than 200 items.
- A static sticker pack can contain no more than 120 items; a video sticker pack, no more than 50.
- The grid can be between `1×1` and `20×20`, with no more than 200 total cells.
- Telegram's cloud Bot API cannot download files larger than 20 MB. For larger files, send an absolute local path accessible from the machine running the bot.
- Adding and using custom emoji packs generally requires Telegram Premium.
- Pack short names must be unique. If a name is already taken, choose another one.

Rendered files remain in the configured output directory even after the ZIP archive is sent to Telegram.

## Project structure

```text
app/
├── bot.py           # Telegram handlers and conversation flow
├── config.py        # Environment configuration
├── download_flow.py # Batch sticker/custom emoji download conversation
├── media.py         # Media type detection and title helpers
├── path_utils.py    # Local path parsing, including Finder-quoted paths
├── renderer.py      # FFmpeg probing, slicing, and encoding
├── sticker_downloader.py # Download metadata and safe output filenames
├── sticker_pack.py  # Custom emoji and regular sticker pack creation
└── tgs_renderer.py  # TGS to alpha-channel ProRes 4444 MOV rendering
tests/
└── test_renderer.py # Unit tests
```

## Testing

Run the syntax check and unit tests:

```bash
python -m compileall app
python -m unittest discover -s tests
```

## Troubleshooting

### FFmpeg or ffprobe is not found

Make sure both commands are installed and available in your `PATH`:

```bash
ffmpeg -version
ffprobe -version
```

### Alpha channel is not detected

Export the source as ProRes 4444 or another format that preserves transparency. Avoid sending the file as a regular Telegram video because the client may recompress it.

### A rendered emoji exceeds the size limit

Reduce `DEFAULT_FPS`, simplify the animation, or shorten `DEFAULT_DURATION` in `.env`.

### Telegram cannot create the pack

Check that the bot has a username, the pack short name is unique, and its final name ends with `_by_<bot_username>`.
