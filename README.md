# TG ART bot

Локальный персональный Telegram-бот: принимает lossless-видео с alpha, делит
кадр на сетку, рендерит каждую ячейку как Telegram video emoji и создаёт готовый
custom emoji pack.

Каждый результат имеет формат WebM/VP9, размер 100×100, до 30 FPS, не длиннее
3 секунд, без аудио и не больше 256 KB.

## Требования

- macOS или Linux;
- Python 3.9+;
- FFmpeg с `libvpx-vp9` (`brew install ffmpeg` на macOS);
- токен бота от [@BotFather](https://t.me/BotFather).

Рекомендуемый исходник: MOV с ProRes 4444 и alpha. Отправляйте видео в Telegram
как документ, иначе клиент может перекодировать его и удалить прозрачность.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env`:

```env
BOT_TOKEN=123456:telegram-token
ALLOWED_USER_ID=123456789
```

Узнать свой Telegram ID можно через [@userinfobot](https://t.me/userinfobot).

## Запуск

```bash
source .venv/bin/activate
python -m app.bot
```

Затем отправьте боту `/start`, исходный файл или полный локальный путь и сетку,
например `5x3` (5 столбцов, 3 строки). Бот последовательно запросит:

1. отображаемое название пака;
2. короткое имя для ссылки (обязательный `_by_<bot_username>` добавится сам);
3. один эмодзи, который будет назначен всем элементам набора.

После рендера бот пришлёт ZIP и создаст ссылку вида
`https://t.me/addemoji/example_by_bot`.

Telegram разрешает до 200 элементов в одном custom emoji pack. Создать такой
набор может любой пользователь, но добавление и использование пользовательских
наборов обычно требует Telegram Premium.

Для файлов больше 20 MB используйте локальный путь: облачный Bot API не даст
боту скачать такой файл. Результаты остаются в `output/` и одновременно
отправляются ZIP-архивом в чат.

## Проверка

```bash
python -m compileall app
python -m unittest discover -s tests
```
