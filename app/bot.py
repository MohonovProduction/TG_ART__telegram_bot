from __future__ import annotations

import asyncio
import html
import re
import secrets
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
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.media import append_title_suffix, detect_source_kind
from app.renderer import (
    RenderError,
    prepare_sticker_files,
    prepare_video_note,
    render_grid,
    render_image_grid,
)
from app.sticker_pack import (
    build_tg_art_grid,
    create_custom_emoji_pack,
    create_sticker_pack,
    make_sticker_set_name,
    parse_custom_emoji_pack_name,
    validate_pack_emoji,
    validate_pack_title,
)


class RenderFlow(StatesGroup):
    waiting_for_mode = State()
    waiting_for_source = State()
    waiting_for_grid = State()
    waiting_for_pack_title = State()
    waiting_for_pack_name = State()
    waiting_for_emoji = State()
    waiting_for_sticker_kind = State()
    collecting_stickers = State()
    waiting_for_sticker_emoji_mode = State()
    waiting_for_common_sticker_emoji = State()
    waiting_for_individual_sticker_emoji = State()
    waiting_for_sticker_pack_title = State()
    waiting_for_sticker_pack_name = State()
    waiting_for_video_note_source = State()
    waiting_for_existing_tg_art_source = State()
    waiting_for_existing_tg_art_grid = State()


router = Router()
settings: Settings
tg_art_previews: Dict[str, Dict[str, Any]] = {}

MODE_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎨 TG Art"), KeyboardButton(text="🖼 Стикер пак")],
        [KeyboardButton(text="🧩 Собрать TG Art"), KeyboardButton(text="⭕ Кружок из видео")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)
STICKER_KIND_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Статичные"), KeyboardButton(text="Видео")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)
EMOJI_MODE_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Один эмодзи для всех"), KeyboardButton(text="Отдельный эмодзи каждому")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def _allowed(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == settings.allowed_user_id)


async def _reject(message: Message) -> None:
    await message.answer("Этот локальный бот доступен только владельцу.")


def _sticker_limit(sticker_format: str) -> int:
    return 120 if sticker_format == "static" else 50


def _natural_path_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


async def _cleanup_temporary_sources(data: Dict[str, Any]) -> None:
    if data.get("temporary") and data.get("source"):
        Path(data["source"]).unlink(missing_ok=True)
    for source in data.get("temporary_sticker_sources", []):
        Path(source).unlink(missing_ok=True)
    if data.get("video_note_temporary") and data.get("video_note_source"):
        Path(data["video_note_source"]).unlink(missing_ok=True)


async def _reset_flow(state: FSMContext) -> None:
    await _cleanup_temporary_sources(await state.get_data())
    await state.clear()


async def _start_emoji_pack(message: Message, state: FSMContext) -> None:
    await _reset_flow(state)
    await state.set_state(RenderFlow.waiting_for_source)
    await message.answer(
        "Отправьте изображение или lossless-видео с alpha <b>как файл</b>. "
        "Также можно прислать полный локальный путь.\n\n"
        "Например: <code>/Users/me/Desktop/art.png</code>",
        reply_markup=ReplyKeyboardRemove(),
    )


async def _start_sticker_pack(message: Message, state: FSMContext) -> None:
    await _reset_flow(state)
    await state.set_state(RenderFlow.waiting_for_sticker_kind)
    await message.answer("Выберите тип стикеров.", reply_markup=STICKER_KIND_KEYBOARD)


async def _start_video_note(message: Message, state: FSMContext) -> None:
    await _reset_flow(state)
    await state.set_state(RenderFlow.waiting_for_video_note_source)
    await message.answer(
        "Отправьте видео или видео <b>как файл</b>. Также можно прислать полный "
        "локальный путь к видео.\n\nВидео будет обрезано по центру до квадрата. "
        "Если оно длиннее 60 секунд, я возьму первые 60 секунд.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def _start_existing_tg_art(message: Message, state: FSMContext) -> None:
    await _reset_flow(state)
    await state.set_state(RenderFlow.waiting_for_existing_tg_art_source)
    await message.answer(
        "Пришлите ссылку на emoji pack вида <code>https://t.me/addemoji/pack_name</code> "
        "или отправьте custom emoji из этого пака. Затем я запрошу размер сетки.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def _set_existing_tg_art_pack(
    message: Message, state: FSMContext, bot: Bot, pack_name: str
) -> None:
    sticker_set = await bot.get_sticker_set(pack_name)
    stickers = [
        {
            "custom_emoji_id": sticker.custom_emoji_id,
            "fallback_emoji": sticker.emoji or "▫️",
        }
        for sticker in sticker_set.stickers
        if sticker.custom_emoji_id
    ]
    if not stickers:
        raise ValueError("В этом наборе нет custom emoji")
    await state.update_data(existing_tg_art_pack_name=pack_name, existing_tg_art_stickers=stickers)
    await state.set_state(RenderFlow.waiting_for_existing_tg_art_grid)
    await message.answer(
        f"Найдено custom emoji: <b>{len(stickers)}</b>. Отправьте размер сетки, например <code>10x8</code>."
    )


async def _set_source(
    message: Message, state: FSMContext, source: Path, temporary: bool, source_kind: str
) -> None:
    await state.update_data(source=str(source), temporary=temporary, source_kind=source_kind)
    await state.set_state(RenderFlow.waiting_for_grid)
    await message.answer(
        "Исходник принят. Отправьте размер сетки в формате <code>5x3</code> "
        "(столбцы × строки)."
    )


async def _start_sticker_collection(message: Message, state: FSMContext, sticker_format: str) -> None:
    await state.update_data(
        sticker_format=sticker_format,
        sticker_sources=[],
        temporary_sticker_sources=[],
    )
    await state.set_state(RenderFlow.collecting_stickers)
    source_kind = "изображения" if sticker_format == "static" else "видео"
    await message.answer(
        f"Отправляйте {source_kind} как файлы. Можно прислать несколько сообщений или "
        "указать путь к папке на компьютере с ботом. Когда закончите, отправьте /done.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def _finish_sticker_collection(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    sources = data.get("sticker_sources", [])
    if not sources:
        await message.answer("Сначала добавьте хотя бы один файл.")
        return
    await state.set_state(RenderFlow.waiting_for_sticker_emoji_mode)
    await message.answer(
        f"Принято файлов: <b>{len(sources)}</b>. Как назначить эмодзи?",
        reply_markup=EMOJI_MODE_KEYBOARD,
    )


async def _ask_sticker_pack_title(message: Message, state: FSMContext) -> None:
    await state.set_state(RenderFlow.waiting_for_sticker_pack_title)
    await message.answer(
        "Как будет называться sticker pack? Например <code>My stickers</code>. "
        f"Я автоматически добавлю <code>{html.escape(settings.pack_title_suffix)}</code>.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def _send_tg_art_preview(bot: Bot, chat_id: int, preview: Dict[str, Any]) -> None:
    """Send the art only after the user has added the pack in Telegram."""
    sticker_set = await bot.get_sticker_set(preview["pack_name"])
    stickers = [sticker for sticker in sticker_set.stickers if sticker.custom_emoji_id]
    custom_emoji_ids = [sticker.custom_emoji_id for sticker in stickers]
    fallback_emojis = [sticker.emoji for sticker in stickers]
    tg_art, entities = build_tg_art_grid(
        custom_emoji_ids,
        int(preview["columns"]),
        int(preview["rows"]),
        fallback_emojis,
    )
    await bot.send_message(chat_id, tg_art, entities=entities, parse_mode=None)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await _reset_flow(state)
    await state.set_state(RenderFlow.waiting_for_mode)
    await message.answer(
        "Что хотите создать?",
        reply_markup=MODE_KEYBOARD,
    )


@router.message(Command("emoji_pack"))
async def emoji_pack_command(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await _start_emoji_pack(message, state)


@router.message(Command("sticker_pack"))
async def sticker_pack_command(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await _start_sticker_pack(message, state)


@router.message(Command("video_note"))
async def video_note_command(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await _start_video_note(message, state)


@router.message(Command("tg_art"))
async def existing_tg_art_command(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await _start_existing_tg_art(message, state)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await _reset_flow(state)
    await message.answer("Отменено. Используйте /start для нового рендера.")


@router.message(RenderFlow.waiting_for_mode, F.text)
async def receive_mode(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    mode = (message.text or "").strip().lower()
    if mode in {"🎨 tg art", "tg art", "art", "эмодзи", "emoji"}:
        await _start_emoji_pack(message, state)
        return
    if mode in {"🖼 стикер пак", "стикер пак", "стикеры", "стикер", "stickers", "sticker"}:
        await _start_sticker_pack(message, state)
        return
    if mode in {"🧩 собрать tg art", "собрать tg art", "собрать арт", "tg art из пака"}:
        await _start_existing_tg_art(message, state)
        return
    if mode in {"⭕ кружок из видео", "кружок из видео", "кружок", "video note"}:
        await _start_video_note(message, state)
        return
    await message.answer("Выберите вариант кнопкой ниже.", reply_markup=MODE_KEYBOARD)


@router.message(RenderFlow.waiting_for_existing_tg_art_source, F.text)
async def receive_existing_tg_art_source(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    custom_emoji_ids = [
        entity.custom_emoji_id
        for entity in (message.entities or [])
        if entity.type == "custom_emoji" and entity.custom_emoji_id
    ]
    try:
        if custom_emoji_ids:
            stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=[custom_emoji_ids[0]])
            if not stickers or not stickers[0].set_name:
                raise ValueError("Не удалось определить набор этого custom emoji")
            pack_name = stickers[0].set_name
        else:
            pack_name = parse_custom_emoji_pack_name(message.text or "")
        await _set_existing_tg_art_pack(message, state, bot, pack_name)
    except ValueError as error:
        await message.answer(html.escape(str(error)))
    except TelegramAPIError as error:
        await message.answer(
            "Telegram не смог получить emoji pack. Проверьте ссылку или доступ к набору.\n\n"
            f"Ошибка: <code>{html.escape(str(error)[:3000])}</code>"
        )


@router.message(RenderFlow.waiting_for_existing_tg_art_grid, F.text)
async def receive_existing_tg_art_grid(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    match = re.fullmatch(r"\s*(\d{1,2})\s*[xх×]\s*(\d{1,2})\s*", message.text or "", re.I)
    if not match:
        await message.answer("Нужен формат <code>10x8</code>: столбцы × строки.")
        return
    columns, rows = map(int, match.groups())
    cell_count = columns * rows
    if not 1 <= columns <= 20 or not 1 <= rows <= 20 or cell_count > 200:
        await message.answer("Размер сетки должен быть от 1×1 до 20×20 и содержать не более 200 ячеек.")
        return
    data = await state.get_data()
    stickers = data["existing_tg_art_stickers"]
    if len(stickers) != cell_count:
        await message.answer(
            f"В паке {len(stickers)} emoji, а в сетке {cell_count} ячеек. Повторяю emoji по кругу."
        )
    grid_stickers = [stickers[index % len(stickers)] for index in range(cell_count)]
    custom_emoji_ids = [sticker["custom_emoji_id"] for sticker in grid_stickers]
    fallback_emojis = [sticker["fallback_emoji"] for sticker in grid_stickers]
    try:
        tg_art, entities = build_tg_art_grid(custom_emoji_ids, columns, rows, fallback_emojis)
        await message.answer(tg_art, entities=entities, parse_mode=None)
    except TelegramAPIError as error:
        await message.answer(f"Telegram не смог отправить TG Art:\n<code>{html.escape(str(error)[:3000])}</code>")
        return
    finally:
        await state.clear()


@router.callback_query(F.data.startswith("tg_art:"))
async def send_tg_art_preview(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or callback.from_user.id != settings.allowed_user_id:
        await callback.answer("Этот бот доступен только владельцу.", show_alert=True)
        return
    token = (callback.data or "").removeprefix("tg_art:")
    preview = tg_art_previews.get(token)
    if not preview or preview["user_id"] != callback.from_user.id:
        await callback.answer("Срок действия кнопки истёк. Создайте TG Art заново.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("Не удалось определить чат для отправки TG Art.", show_alert=True)
        return
    try:
        await _send_tg_art_preview(bot, callback.message.chat.id, preview)
    except TelegramAPIError as error:
        await callback.answer("Telegram не смог отправить TG Art.", show_alert=True)
        await bot.send_message(
            callback.message.chat.id,
            f"Не удалось отправить TG Art: <code>{html.escape(str(error)[:3000])}</code>",
        )
        return
    except (ValueError, RuntimeError) as error:
        await callback.answer("Не удалось собрать TG Art.", show_alert=True)
        await bot.send_message(
            callback.message.chat.id,
            f"Не удалось собрать TG Art: <code>{html.escape(str(error)[:3000])}</code>",
        )
        return
    await callback.answer()


async def _render_and_send_video_note(
    message: Message,
    state: FSMContext,
    source: Path,
    temporary: bool,
) -> None:
    await state.update_data(
        video_note_source=str(source),
        video_note_temporary=temporary,
    )
    await message.answer("Готовлю кружок…")
    result = None
    try:
        result = await prepare_video_note(source, settings.temp_dir)
        if result.truncated:
            await message.answer(
                "Исходное видео длиннее 60 секунд — в кружок вошли первые 60 секунд."
            )
        await message.answer_video_note(
            video_note=FSInputFile(result.file),
            duration=max(1, round(result.duration)),
            length=640,
        )
    except RenderError as error:
        await message.answer(
            f"Не удалось подготовить кружок:\n<code>{html.escape(str(error)[:3500])}</code>"
        )
    except TelegramAPIError as error:
        await message.answer(
            f"Telegram не смог отправить кружок:\n<code>{html.escape(str(error)[:3000])}</code>"
        )
    except Exception as error:
        await message.answer(
            f"Не удалось создать кружок:\n<code>{html.escape(str(error)[:3000])}</code>"
        )
    finally:
        if result is not None:
            shutil.rmtree(result.directory, ignore_errors=True)
        if temporary:
            source.unlink(missing_ok=True)
        await state.clear()


@router.message(RenderFlow.waiting_for_video_note_source, F.video)
async def receive_video_note_video(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    video = message.video
    assert video is not None
    destination = settings.temp_dir / f"{message.from_user.id}_{video.file_unique_id}.mp4"
    try:
        await bot.download(video, destination=destination)
    except Exception as error:
        destination.unlink(missing_ok=True)
        await message.answer(
            "Не удалось скачать видео через Bot API. Для большого файла пришлите "
            "полный локальный путь.\n\n"
            f"Ошибка: <code>{type(error).__name__}</code>"
        )
        return
    await _render_and_send_video_note(message, state, destination, temporary=True)


@router.message(RenderFlow.waiting_for_video_note_source, F.document)
async def receive_video_note_document(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    document = message.document
    assert document is not None
    suffix = Path(document.file_name or "video.mp4").suffix or ".mp4"
    destination = settings.temp_dir / f"{message.from_user.id}_{document.file_unique_id}{suffix}"
    try:
        source_kind = detect_source_kind(destination, document.mime_type)
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    if source_kind != "video":
        await message.answer("Для кружка нужно отправить видео.")
        return
    try:
        await bot.download(document, destination=destination)
    except Exception as error:
        destination.unlink(missing_ok=True)
        await message.answer(
            "Не удалось скачать файл через Bot API. Для большого файла пришлите "
            "полный локальный путь.\n\n"
            f"Ошибка: <code>{type(error).__name__}</code>"
        )
        return
    await _render_and_send_video_note(message, state, destination, temporary=True)


@router.message(RenderFlow.waiting_for_video_note_source, F.text)
async def receive_video_note_path(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    source = Path((message.text or "").strip()).expanduser().resolve()
    if not source.is_file():
        await message.answer("Файл не найден. Проверьте полный путь и попробуйте ещё раз.")
        return
    try:
        source_kind = detect_source_kind(source)
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    if source_kind != "video":
        await message.answer("Для кружка нужен путь к видеофайлу.")
        return
    await _render_and_send_video_note(message, state, source, temporary=False)


@router.message(RenderFlow.waiting_for_sticker_kind, F.text)
async def receive_sticker_kind(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    value = (message.text or "").strip().lower()
    if value in {"статичные", "статические", "static"}:
        await _start_sticker_collection(message, state, "static")
        return
    if value in {"видео", "video"}:
        await _start_sticker_collection(message, state, "video")
        return
    await message.answer("Выберите <b>Статичные</b> или <b>Видео</b>.", reply_markup=STICKER_KIND_KEYBOARD)


@router.message(RenderFlow.collecting_stickers, Command("done"))
async def finish_sticker_collection(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await _finish_sticker_collection(message, state)


@router.message(RenderFlow.collecting_stickers, F.document)
async def receive_sticker_document(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    document = message.document
    assert document is not None
    data = await state.get_data()
    sticker_format = data["sticker_format"]
    limit = _sticker_limit(sticker_format)
    sources = list(data.get("sticker_sources", []))
    if len(sources) >= limit:
        await message.answer(f"В этом наборе может быть не больше {limit} стикеров.")
        return

    suffix = Path(document.file_name or "sticker").suffix
    destination = settings.temp_dir / f"{message.from_user.id}_{document.file_unique_id}{suffix}"
    try:
        source_kind = detect_source_kind(destination, document.mime_type)
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    expected_kind = "image" if sticker_format == "static" else "video"
    if source_kind != expected_kind:
        expected_label = "изображение" if expected_kind == "image" else "видео"
        await message.answer(f"Для этого набора нужно отправить {expected_label}.")
        return
    try:
        await bot.download(document, destination=destination)
    except Exception as error:
        await message.answer(f"Не удалось скачать файл: <code>{type(error).__name__}</code>")
        return

    sources.append(str(destination))
    temporary_sources = list(data.get("temporary_sticker_sources", []))
    temporary_sources.append(str(destination))
    await state.update_data(sticker_sources=sources, temporary_sticker_sources=temporary_sources)
    await message.answer(f"Добавлено: {len(sources)}/{limit}. Отправьте ещё файлы или /done.")


@router.message(RenderFlow.collecting_stickers, F.text)
async def receive_sticker_folder(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    folder = Path((message.text or "").strip()).expanduser().resolve()
    if not folder.is_dir():
        await message.answer("Отправьте файл, путь к папке или /done.")
        return
    data = await state.get_data()
    sticker_format = data["sticker_format"]
    expected_kind = "image" if sticker_format == "static" else "video"
    limit = _sticker_limit(sticker_format)
    sources = list(data.get("sticker_sources", []))
    accepted: list[Path] = []
    for path in sorted((item for item in folder.iterdir() if item.is_file()), key=_natural_path_key):
        try:
            if detect_source_kind(path) == expected_kind:
                accepted.append(path)
        except ValueError:
            continue
    remaining = limit - len(sources)
    if not accepted:
        await message.answer("В папке нет файлов подходящего формата.")
        return
    if len(accepted) > remaining:
        await message.answer(f"В папке {len(accepted)} файлов, но можно добавить только {remaining}.")
        return
    sources.extend(str(path) for path in accepted)
    await state.update_data(sticker_sources=sources)
    await message.answer(f"Добавлено из папки: {len(accepted)}. Всего: {len(sources)}/{limit}. Отправьте /done.")


@router.message(RenderFlow.waiting_for_sticker_emoji_mode, F.text)
async def receive_sticker_emoji_mode(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    value = (message.text or "").strip().lower()
    if value in {"один эмодзи для всех", "один", "общий"}:
        await state.set_state(RenderFlow.waiting_for_common_sticker_emoji)
        await message.answer("Отправьте один эмодзи для всех стикеров.", reply_markup=ReplyKeyboardRemove())
        return
    if value in {"отдельный эмодзи каждому", "отдельный", "каждому"}:
        await state.update_data(sticker_emojis=[], individual_emoji_index=0)
        await state.set_state(RenderFlow.waiting_for_individual_sticker_emoji)
        await message.answer("Отправьте эмодзи для стикера 1.", reply_markup=ReplyKeyboardRemove())
        return
    await message.answer("Выберите способ назначения эмодзи.", reply_markup=EMOJI_MODE_KEYBOARD)


@router.message(RenderFlow.waiting_for_common_sticker_emoji, F.text)
async def receive_common_sticker_emoji(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    try:
        emoji = validate_pack_emoji(message.text or "")
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    data = await state.get_data()
    await state.update_data(sticker_emojis=[emoji] * len(data["sticker_sources"]))
    await _ask_sticker_pack_title(message, state)


@router.message(RenderFlow.waiting_for_individual_sticker_emoji, F.text)
async def receive_individual_sticker_emoji(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    try:
        emoji = validate_pack_emoji(message.text or "")
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    data = await state.get_data()
    emojis = list(data.get("sticker_emojis", []))
    emojis.append(emoji)
    total = len(data["sticker_sources"])
    if len(emojis) == total:
        await state.update_data(sticker_emojis=emojis)
        await _ask_sticker_pack_title(message, state)
        return
    await state.update_data(sticker_emojis=emojis, individual_emoji_index=len(emojis))
    await message.answer(f"Отправьте эмодзи для стикера {len(emojis) + 1} из {total}.")


@router.message(RenderFlow.waiting_for_sticker_pack_title, F.text)
async def receive_sticker_pack_title(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    try:
        title = validate_pack_title(append_title_suffix(message.text or "", settings.pack_title_suffix))
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    await state.update_data(sticker_pack_title=title)
    await state.set_state(RenderFlow.waiting_for_sticker_pack_name)
    await message.answer("Введите короткое имя ссылки, например <code>my_stickers</code>.")


@router.message(RenderFlow.waiting_for_sticker_pack_name, F.text)
async def receive_sticker_pack_name(message: Message, state: FSMContext, bot: Bot) -> None:
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

    data: Dict[str, Any] = await state.get_data()
    sources = [Path(source) for source in data["sticker_sources"]]
    sticker_format = data["sticker_format"]
    await message.answer(f"Конвертирую {len(sources)} стикеров. Это может занять несколько минут…")
    try:
        result = await prepare_sticker_files(
            sources=sources,
            sticker_format=sticker_format,
            output_root=settings.output_dir,
            fps=settings.default_fps,
            duration=settings.default_duration,
            max_static_size_kb=settings.max_static_sticker_size_kb,
            max_video_size_kb=settings.max_video_sticker_size_kb,
        )
        await message.answer_document(
            FSInputFile(result.archive),
            caption=f"Готово: {len(result.files)} файлов {sticker_format}. Локальная папка: <code>{result.directory}</code>",
        )
        status = await message.answer(f"Создаю sticker pack: загружено 0/{len(result.files)}…")

        async def report_progress(completed: int, total: int) -> None:
            if completed == total or completed == 1 or completed % 5 == 0:
                await status.edit_text(f"Создаю sticker pack: загружено {completed}/{total}…")

        pack_url = await create_sticker_pack(
            bot=bot,
            user_id=settings.allowed_user_id,
            files=result.files,
            title=data["sticker_pack_title"],
            name=pack_name,
            emojis=data["sticker_emojis"],
            sticker_format=sticker_format,
            progress=report_progress,
        )
        await status.edit_text(
            f"Sticker pack создан: <a href=\"{pack_url}\">{html.escape(data['sticker_pack_title'])}</a>"
        )
    except RenderError as error:
        await message.answer(f"Конвертация не выполнена:\n<code>{html.escape(str(error)[:3500])}</code>")
    except TelegramAPIError as error:
        await message.answer(f"Telegram не смог создать пак:\n<code>{html.escape(str(error)[:3000])}</code>")
    except Exception as error:
        await message.answer(f"Не удалось завершить создание пака:\n<code>{html.escape(str(error)[:3000])}</code>")
    finally:
        await _cleanup_temporary_sources(data)
        await state.clear()


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
        source_kind = detect_source_kind(destination, document.mime_type)
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    try:
        await bot.download(document, destination=destination)
    except Exception as error:
        await message.answer(
            "Не удалось скачать файл через Bot API. Для файлов больше 20 MB "
            "пришлите локальный путь к исходнику.\n\n"
            f"Ошибка: <code>{type(error).__name__}</code>"
        )
        return
    await _set_source(message, state, destination, temporary=True, source_kind=source_kind)


@router.message(RenderFlow.waiting_for_source, F.photo)
async def receive_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    photo = message.photo[-1]
    destination = settings.temp_dir / f"{message.from_user.id}_{photo.file_unique_id}.jpg"
    await bot.download(photo, destination=destination)
    await _set_source(message, state, destination, temporary=True, source_kind="image")


@router.message(RenderFlow.waiting_for_source, F.text)
async def receive_path(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    source = Path((message.text or "").strip()).expanduser().resolve()
    if not source.is_file():
        await message.answer("Файл не найден. Проверьте полный путь и попробуйте ещё раз.")
        return
    try:
        source_kind = detect_source_kind(source)
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    await _set_source(message, state, source, temporary=False, source_kind=source_kind)


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
        f"<code>My art</code>. Я автоматически добавлю "
        f"<code>{html.escape(settings.pack_title_suffix)}</code>."
    )


@router.message(RenderFlow.waiting_for_pack_title, F.text)
async def receive_pack_title(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    try:
        title = validate_pack_title(
            append_title_suffix(message.text or "", settings.pack_title_suffix)
        )
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
    source_kind = data["source_kind"]
    columns, rows = int(data["columns"]), int(data["rows"])
    await message.answer(f"Рендерю сетку {columns}×{rows}. Это может занять несколько минут…")
    try:
        if source_kind == "image":
            result = await render_image_grid(
                source=source,
                columns=columns,
                rows=rows,
                output_root=settings.output_dir,
            )
        else:
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
                f"Готово: {len(result.files)} файлов "
                f"{'PNG' if source_kind == 'image' else 'WebM'}.\n"
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
            sticker_format="static" if source_kind == "image" else "video",
            progress=report_progress,
        )
        preview_token = secrets.token_urlsafe(8)
        tg_art_previews[preview_token] = {
            "user_id": settings.allowed_user_id,
            "pack_name": data["pack_name"],
            "columns": columns,
            "rows": rows,
            "fallback_emoji": emoji,
        }
        await status.edit_text(
            f"Emoji pack создан: <a href=\"{pack_url}\">{html.escape(data['pack_title'])}</a>\n\n"
            "Добавьте пак в Telegram, затем нажмите «Показать TG Art».",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить пак", url=pack_url)],
                    [
                        InlineKeyboardButton(
                            text="🎨 Показать TG Art",
                            callback_data=f"tg_art:{preview_token}",
                        )
                    ],
                ]
            ),
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
    await bot.set_my_commands(
        [
            BotCommand(command="emoji_pack", description="Создать эмодзи-пак"),
            BotCommand(command="sticker_pack", description="Создать стикерпак"),
            BotCommand(command="tg_art", description="Собрать TG Art из emoji pack"),
            BotCommand(command="video_note", description="Сделать кружок из видео"),
            BotCommand(command="cancel", description="Отменить текущую операцию"),
        ]
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
