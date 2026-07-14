from __future__ import annotations

import asyncio
import html
import re
import shutil
from pathlib import Path
from typing import Any, Dict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, Message
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.renderer import RenderError, render_grid
from app.sticker_pack import (
    create_custom_emoji_pack,
    make_sticker_set_name,
    validate_pack_emoji,
    validate_pack_title,
)


class RenderFlow(StatesGroup):
    waiting_for_source = State()
    waiting_for_grid = State()
    waiting_for_pack_title = State()
    waiting_for_pack_name = State()
    waiting_for_emoji = State()


router = Router()
settings: Settings


def _allowed(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == settings.allowed_user_id)


async def _reject(message: Message) -> None:
    await message.answer("Этот локальный бот доступен только владельцу.")


async def _set_source(message: Message, state: FSMContext, source: Path, temporary: bool) -> None:
    await state.update_data(source=str(source), temporary=temporary)
    await state.set_state(RenderFlow.waiting_for_grid)
    await message.answer(
        "Исходник принят. Отправьте размер сетки в формате <code>5x3</code> "
        "(столбцы × строки)."
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await state.clear()
    await state.set_state(RenderFlow.waiting_for_source)
    await message.answer(
        "Отправьте lossless-видео с alpha <b>как файл</b> или пришлите полный "
        "локальный путь к нему.\n\nНапример: <code>/Users/me/Desktop/art.mov</code>"
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    data = await state.get_data()
    if data.get("temporary") and data.get("source"):
        Path(data["source"]).unlink(missing_ok=True)
    await state.clear()
    await message.answer("Отменено. Используйте /start для нового рендера.")


@router.message(RenderFlow.waiting_for_source, F.document)
async def receive_document(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    document = message.document
    assert document is not None
    suffix = Path(document.file_name or "source.mov").suffix or ".mov"
    destination = settings.temp_dir / f"{message.from_user.id}_{document.file_unique_id}{suffix}"
    try:
        await bot.download(document, destination=destination)
    except Exception as error:
        await message.answer(
            "Не удалось скачать файл через Bot API. Для файлов больше 20 MB "
            "пришлите локальный путь к исходнику.\n\n"
            f"Ошибка: <code>{type(error).__name__}</code>"
        )
        return
    await _set_source(message, state, destination, temporary=True)


@router.message(RenderFlow.waiting_for_source, F.text)
async def receive_path(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    source = Path((message.text or "").strip()).expanduser().resolve()
    if not source.is_file():
        await message.answer("Файл не найден. Проверьте полный путь и попробуйте ещё раз.")
        return
    await _set_source(message, state, source, temporary=False)


@router.message(RenderFlow.waiting_for_grid, F.text)
async def receive_grid(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    match = re.fullmatch(r"\s*(\d{1,2})\s*[xх×]\s*(\d{1,2})\s*", message.text or "", re.I)
    if not match:
        await message.answer("Нужен формат <code>5x3</code>: столбцы × строки.")
        return
    columns, rows = map(int, match.groups())
    if not 1 <= columns <= 20 or not 1 <= rows <= 20:
        await message.answer("Размер сетки должен быть от 1×1 до 20×20.")
        return
    if columns * rows > 200:
        await message.answer("В одном custom emoji pack может быть не больше 200 элементов.")
        return
    await state.update_data(columns=columns, rows=rows)
    await state.set_state(RenderFlow.waiting_for_pack_title)
    await message.answer(
        "Как будет называться пак? Это отображаемое название, например "
        "<code>My animated art</code>."
    )


@router.message(RenderFlow.waiting_for_pack_title, F.text)
async def receive_pack_title(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    try:
        title = validate_pack_title(message.text or "")
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    await state.update_data(pack_title=title)
    await state.set_state(RenderFlow.waiting_for_pack_name)
    await message.answer(
        "Выберите имя ссылки — только английские буквы, цифры и подчёркивание.\n"
        "Например <code>summer_art</code>. Обязательный суффикс с username бота "
        "я добавлю автоматически."
    )


@router.message(RenderFlow.waiting_for_pack_name, F.text)
async def receive_pack_name(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    bot_user = await bot.get_me()
    if not bot_user.username:
        await message.answer("У бота должен быть username. Задайте его через @BotFather.")
        return
    try:
        pack_name = make_sticker_set_name(message.text or "", bot_user.username)
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    await state.update_data(pack_name=pack_name)
    await state.set_state(RenderFlow.waiting_for_emoji)
    await message.answer(
        "Теперь отправьте <b>один эмодзи</b>. Он будет назначен всем video emoji "
        "в этом паке, например 🎨."
    )


@router.message(RenderFlow.waiting_for_emoji, F.text)
async def receive_emoji(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    try:
        emoji = validate_pack_emoji(message.text or "")
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return

    data: Dict[str, Any] = await state.get_data()
    source = Path(data["source"])
    columns, rows = int(data["columns"]), int(data["rows"])
    await message.answer(f"Рендерю сетку {columns}×{rows}. Это может занять несколько минут…")
    try:
        result = await render_grid(
            source=source,
            columns=columns,
            rows=rows,
            output_root=settings.output_dir,
            fps=settings.default_fps,
            duration=settings.default_duration,
            max_size_kb=settings.max_emoji_size_kb,
        )
        await message.answer_document(
            FSInputFile(result.archive),
            caption=(
                f"Готово: {len(result.files)} файлов WebM.\n"
                f"Исходник: {result.source_info.width}×{result.source_info.height}, "
                f"{result.source_info.pixel_format}.\n"
                f"Локальная папка: <code>{result.directory}</code>"
            ),
        )
        status = await message.answer(f"Создаю emoji pack: загружено 0/{len(result.files)}…")
        last_reported = 0

        async def report_progress(completed: int, total: int) -> None:
            nonlocal last_reported
            if completed == total or completed - last_reported >= 5:
                last_reported = completed
                await status.edit_text(f"Создаю emoji pack: загружено {completed}/{total}…")

        pack_url = await create_custom_emoji_pack(
            bot=bot,
            user_id=settings.allowed_user_id,
            files=result.files,
            title=data["pack_title"],
            name=data["pack_name"],
            emoji=emoji,
            progress=report_progress,
        )
        await status.edit_text(
            f"Emoji pack создан: <a href=\"{pack_url}\">{html.escape(data['pack_title'])}</a>"
        )
    except RenderError as error:
        await message.answer(f"Рендер не выполнен:\n<code>{html.escape(str(error)[:3500])}</code>")
    except TelegramAPIError as error:
        await message.answer(
            "Telegram не смог создать пак. Если набор уже появился, часть emoji могла "
            "успеть загрузиться.\n\n"
            f"Ошибка: <code>{html.escape(str(error)[:3000])}</code>"
        )
    except Exception as error:
        await message.answer(
            "Не удалось завершить создание пака.\n\n"
            f"Ошибка: <code>{html.escape(str(error)[:3000])}</code>"
        )
    finally:
        if data.get("temporary"):
            source.unlink(missing_ok=True)
        await state.clear()


@router.message()
async def fallback(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await message.answer("Используйте /start, чтобы начать новый рендер, или /cancel для отмены.")


async def main() -> None:
    global settings
    settings = Settings.from_env()
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("FFmpeg и ffprobe не найдены в PATH")
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
