#!/bin/zsh

# Запуск Telegram-бота без IDE. Этот файл можно открыть двойным щелчком в Finder.
set -u

PROJECT_DIR="${0:A:h}"
PYTHON="$PROJECT_DIR/.venv/bin/python"

# Spotlight/Terminal can start with a minimal PATH; include standard Homebrew locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

print "\nЗапуск TG ART Bot…\n"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  print "Ошибка: не найден файл .env в $PROJECT_DIR"
  print "Создайте его из .env.example и укажите BOT_TOKEN и ALLOWED_USER_ID."
  read -r "?Нажмите Enter, чтобы закрыть окно…"
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  print "Ошибка: не найдено виртуальное окружение .venv."
  print "Выполните один раз: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  read -r "?Нажмите Enter, чтобы закрыть окно…"
  exit 1
fi

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  print "Ошибка: не найдены ffmpeg или ffprobe в PATH."
  print "Установите их командой: brew install ffmpeg"
  read -r "?Нажмите Enter, чтобы закрыть окно…"
  exit 1
fi

if pgrep -f "$PYTHON -m app.bot" >/dev/null 2>&1; then
  print "TG ART Bot уже запущен — второе окно не требуется."
  read -r "?Нажмите Enter, чтобы закрыть окно…"
  exit 0
fi

cd "$PROJECT_DIR" || exit 1
"$PYTHON" -m app.bot
exit_code=$?

print "\nБот остановлен (код $exit_code)."
read -r "?Нажмите Enter, чтобы закрыть окно…"
exit "$exit_code"
