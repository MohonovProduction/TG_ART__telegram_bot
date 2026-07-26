# TG ART Bot

A personal Telegram bot that creates custom-emoji art grids and ordinary static or video sticker packs.

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
- Restricts access to a single configured Telegram user
- Accepts Telegram uploads or local file paths

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
| `ALLOWED_USER_ID` | Required | The only Telegram user allowed to use the bot |
| `OUTPUT_DIR` | `./output` | Directory for rendered tiles and ZIP archives |
| `TEMP_DIR` | `./temp` | Directory for temporary uploaded files |
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

### Quick launch on macOS

After installation, open `run-bot.command` with a double click in Finder. It starts
the bot in Terminal and keeps the log window open if it stops.

`TG ART Bot.app` is a Spotlight-friendly launcher. Move it to `/Applications` once:

```bash
cp -R "TG ART Bot.app" /Applications/
```

Then press `Command + Space`, type `TG ART Bot`, and press Enter. The app opens a
Terminal window with the bot. The launcher expects this project to remain at
`/Users/mohonovproduction/Documents/TG_ART_bot`.

## Usage

1. Send `/start` and choose **TG Art** or **Стикер пак**.
2. For **TG Art**, upload an image or an alpha-channel video as a document (or provide a local file path), enter the grid size, pack title, link name, and one emoji.
3. For **Стикер пак**, choose **Статичные** or **Видео**.
4. Send every source file as a document, or provide an absolute path to a folder on the computer running the bot. Send `/done` when the list is complete.
5. Choose one common emoji or assign an emoji to every sticker in sequence, then enter the title and link name.

After rendering, the bot sends a ZIP archive and creates a pack link similar to:

```text
https://t.me/addemoji/example_by_bot
```

Regular sticker packs use a link such as:

```text
https://t.me/addstickers/example_by_bot
```

Use `/cancel` at any point to stop the current operation.

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

Regular video stickers are converted to WebM/VP9 without audio, with a maximum duration of 3 seconds and 30 FPS. Static stickers are converted to WebP. Both formats use a 512×512 canvas.

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
├── media.py         # Media type detection and title helpers
├── renderer.py      # FFmpeg probing, slicing, and encoding
└── sticker_pack.py  # Custom emoji and regular sticker pack creation
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
