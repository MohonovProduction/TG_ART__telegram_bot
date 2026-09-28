from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import shutil
from pathlib import Path
from typing import Any, Dict
from types import SimpleNamespace

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
    BotCommandScopeChat,
)
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app import access, admin, analysis_flow, inbox_flow, video_note_flow
from app.admin import router as admin_router
from app.pack_names import available_pack_name
from app.grid_keyboard import grid_keyboard, confirmed_grid
from app.sticker_pack import suggest_pack_name
from app.download_flow import configure as configure_download_flow
from app.download_flow import router as download_router
from app.download_flow import start_download
from app.media import append_title_suffix, detect_source_kind
from app.path_utils import parse_local_path
from app.archive_delivery import send_zip_parts
from app.jobs import HeavyJobQueue, JobAlreadyRunning, JobCancelled, PreviewCache, cleanup_expired_job_directories
from app.recipes import Recipe, RecipeCache
from app.activity_log import ActivityLogger, AuditMiddleware
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
logger = logging.getLogger(__name__)
settings: Settings
tg_art_previews = PreviewCache()
heavy_jobs: HeavyJobQueue
recipes: RecipeCache
activity_logger: ActivityLogger

BASE_MODE_ROWS = [
    [KeyboardButton(text="🎨 TG Art"), KeyboardButton(text="🖼 Стикер пак")],
    [KeyboardButton(text="🧩 Собрать TG Art"), KeyboardButton(text="⭕ Кружок из видео")],
    [KeyboardButton(text="📥 Скачать стикеры / эмодзи")],
    [KeyboardButton(text="🔍 Анализ поста")],
]

MODE_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        *BASE_MODE_ROWS,
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def mode_keyboard_for(user_id: int) -> ReplyKeyboardMarkup:
    rows = [*BASE_MODE_ROWS]
    if access.store and access.store.role(user_id) in ("owner", "admin"):
        rows.append([KeyboardButton(text="👥 Пользователи")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)


def commands_for(user_id: int) -> list[BotCommand]:
    commands = [
        BotCommand(command="analyze", description="Анализ поста"),
        BotCommand(command="emoji_pack", description="Создать эмодзи-пак"),
        BotCommand(command="sticker_pack", description="Создать стикерпак"),
        BotCommand(command="tg_art", description="Собрать TG Art из emoji pack"),
        BotCommand(command="video_note", description="Сделать кружок из видео"),
        BotCommand(command="download", description="Скачать стикеры и эмодзи"),
        BotCommand(command="cancel", description="Отменить текущую операцию"),
    ]
    if access.store and access.store.role(user_id) in ("owner", "admin"):
        commands.insert(1, BotCommand(command="users", description="Управление пользователями"))
    return commands


async def refresh_commands(bot: Bot, user_id: int) -> None:
    await bot.set_my_commands(commands_for(user_id), scope=BotCommandScopeChat(chat_id=user_id))
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
    return bool(message.from_user and access.allowed(message.from_user.id))


async def _card(message: Message, state: FSMContext, text: str, reply_markup=None) -> Message:
    """Keep exactly one current operation card, always visible at chat bottom.

    Telegram leaves an edited message at its original position and can't edit reply
    keyboards. Replacing the previous bot-owned card keeps the chat clean *and*
    makes the next question discoverable.
    """
    data = await state.get_data()
    message_id = data.get("operation_message_id")
    if message_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=message_id)
        except TelegramAPIError:
            pass
    card = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(operation_message_id=card.message_id)
    return card


async def _set_card_markup(message: Message, state: FSMContext, text: str, reply_markup=None) -> Message:
    return await _card(message, state, text, reply_markup)


async def _chat_action(message: Message, action: str):
    """Return a task that refreshes Telegram's five-second activity indicator."""
    async def pulse() -> None:
        while True:
            try:
                await message.bot.send_chat_action(message.chat.id, action=action)
            except TelegramAPIError:
                return
            await asyncio.sleep(4)
    task = asyncio.create_task(pulse())
    return task


async def _stop_chat_action(task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _reject(message: Message) -> None:
    await message.answer("Доступ закрыт. Обратитесь к владельцу бота.")


def _sticker_limit(sticker_format: str) -> int:
    return 120 if sticker_format == "static" else 50


def _natural_path_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def _source_bytes(sources: list[str]) -> int:
    total = 0
    for source in sources:
        try:
            total += Path(source).stat().st_size
        except OSError:
            continue
    return total


def _fits_job_limit(current_bytes: int, incoming_bytes: int) -> bool:
    return current_bytes + incoming_bytes <= settings.max_job_input_mb * 1024 * 1024


async def _reject_oversized_job(message: Message) -> None:
    await message.answer(
        f"Лимит исходников для одной задачи — {settings.max_job_input_mb} MB. "
        "Завершите текущую пачку или отправьте меньший набор."
    )


async def _cleanup_temporary_sources(data: Dict[str, Any]) -> None:
    if data.get("inbox_resume_data"):
        await _cleanup_temporary_sources(data["inbox_resume_data"])
    if data.get("temporary") and data.get("source"):
        Path(data["source"]).unlink(missing_ok=True)
    for source in data.get("temporary_sticker_sources", []):
        Path(source).unlink(missing_ok=True)
    if data.get("video_note_temporary") and data.get("video_note_source"):
        Path(data["video_note_source"]).unlink(missing_ok=True)


async def _reset_flow(state: FSMContext) -> None:
    await _cleanup_temporary_sources(await state.get_data())
    await state.clear()


async def _run_heavy_job(message: Message, work):
    async def queued(position: int) -> None:
        await message.answer(f"Задача в очереди, позиция: <b>{position}</b>. Начну, когда освободится место.")

    user_id = message.from_user.id
    logger.info("Heavy job queued: user=%s", user_id)
    try:
        result = await heavy_jobs.run(user_id, queued, work)
        logger.info("Heavy job completed: user=%s", user_id)
        return result
    except Exception:
        logger.exception("Heavy job failed: user=%s", user_id)
        raise


async def _start_emoji_pack(message: Message, state: FSMContext) -> None:
    await _reset_flow(state)
    await state.set_state(RenderFlow.waiting_for_source)
    await _card(message, state,
        "Отправьте изображение или видео <b>как файл</b>. Прозрачность сохраняется, "
        "но обычное видео без alpha тоже поддерживается. "
        + ("Также можно прислать полный локальный путь.\n\n"
        "Например: <code>/Users/me/Desktop/art.png</code>" if settings.allow_local_paths and message.from_user.id == settings.allowed_user_id else ""),
        reply_markup=ReplyKeyboardRemove(),
    )
    await activity_logger.event(message.bot, message.from_user, "TG Art", "начат сценарий")


async def _start_sticker_pack(message: Message, state: FSMContext) -> None:
    await _reset_flow(state)
    await state.set_state(RenderFlow.waiting_for_sticker_kind)
    await message.answer("Выберите тип стикеров.", reply_markup=STICKER_KIND_KEYBOARD)


async def _start_video_note(message: Message, state: FSMContext) -> None:
    await _reset_flow(state)
    await state.set_state(RenderFlow.waiting_for_video_note_source)
    await message.answer(
        "Отправьте видео или видео <b>как файл</b>. "
        + ("Также можно прислать полный локальный путь к видео. " if settings.allow_local_paths and message.from_user.id == settings.allowed_user_id else "")
        + "\n\nЗатем выберите Cover, Fit или Fill и положение либо фон. "
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
        f"Найдено custom emoji: <b>{len(stickers)}</b>. Выберите правый нижний угол сетки или введите <code>10x8</code>.",
        reply_markup=grid_keyboard()
    )


async def _set_source(
    message: Message, state: FSMContext, source: Path, temporary: bool, source_kind: str
) -> None:
    await state.update_data(source=str(source), temporary=temporary, source_kind=source_kind)
    data = await state.get_data()
    recipe = data.get("recipe")
    if recipe:
        if recipe.kind != "tg_art" or recipe.columns is None or recipe.rows is None:
            raise ValueError("Этот рецепт нельзя использовать для TG Art")
        await state.update_data(
            columns=recipe.columns, rows=recipe.rows, pack_title=recipe.title,
        )
        await state.set_state(RenderFlow.waiting_for_pack_name)
        await _suggest_name(message, state, base=recipe.title)
        return
    await state.set_state(RenderFlow.waiting_for_grid)
    await _card(message, state,
        "Исходник принят. Отправьте размер сетки в формате <code>5x3</code> "
        "(столбцы × строки), либо выберите правый нижний угол кнопкой.",
        reply_markup=grid_keyboard(),
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
        f"Отправляйте {source_kind} как файлы. Можно прислать несколько сообщений. "
        + ("Также можно указать путь к папке на компьютере с ботом. " if settings.allow_local_paths and message.from_user.id == settings.allowed_user_id else "")
        + "Когда закончите, отправьте /done.",
        reply_markup=inbox_flow.COLLECT_KEYBOARD,
    )


async def _finish_sticker_collection(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    sources = data.get("sticker_sources", [])
    if not sources:
        await message.answer("Сначала добавьте хотя бы один файл.")
        return
    data = await state.get_data()
    recipe = data.get("recipe")
    if recipe and recipe.kind == "sticker" and len(recipe.emojis) == len(sources):
        await state.update_data(sticker_emojis=list(recipe.emojis), sticker_pack_title=recipe.title)
        await state.set_state(RenderFlow.waiting_for_sticker_pack_name)
        await _suggest_name(message, state, base=recipe.title)
        return
    await state.set_state(RenderFlow.waiting_for_sticker_emoji_mode)
    await message.answer(
        f"Принято файлов: <b>{len(sources)}</b>. Как назначить эмодзи?",
        reply_markup=EMOJI_MODE_KEYBOARD,
    )


async def _ask_sticker_pack_title(message: Message, state: FSMContext) -> None:
    await state.set_state(RenderFlow.waiting_for_sticker_pack_title)
    await _card(message, state,
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
    await refresh_commands(message.bot, message.from_user.id)
    await message.answer(
        "Что хотите создать?",
        reply_markup=mode_keyboard_for(message.from_user.id),
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
    if mode in {"🔍 анализ поста", "анализ поста"}:
        await analysis_flow.start_analysis(message, state)
        return
    if mode in {"👥 пользователи", "пользователи"}:
        if access.store.role(message.from_user.id) in ("owner", "admin"):
            await admin.show_users(message, state)
        else:
            await _reject(message)
        return
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
    if mode in {
        "📥 скачать стикеры / эмодзи",
        "скачать стикеры / эмодзи",
        "скачать стикеры",
        "download",
    }:
        await start_download(message, state)
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
    selection = await confirmed_grid(message, state)
    if selection is None:
        return
    columns, rows = selection
    cell_count = columns * rows
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
        await message.answer(tg_art, entities=entities, parse_mode=None, reply_markup=ReplyKeyboardRemove())
    except TelegramAPIError as error:
        await message.answer(f"Telegram не смог отправить TG Art:\n<code>{html.escape(str(error)[:3000])}</code>")
        return
    finally:
        await state.clear()


@router.callback_query(F.data.startswith("tg_art:"))
async def send_tg_art_preview(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    if not callback.from_user or not access.allowed(callback.from_user.id):
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
        await _reset_flow(state)
        await _set_existing_tg_art_pack(callback.message, state, bot, preview["pack_name"])
        columns, rows = int(preview["columns"]), int(preview["rows"])
        await state.update_data(pending_grid=[columns, rows])
        await callback.message.answer(
            f"Исходная сетка {columns}×{rows}. Подтвердите её или выберите другую клетку.",
            reply_markup=grid_keyboard(columns, rows),
        )
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
    tg_art_previews.discard(token)


def _recipe_accessible(recipe: Recipe, user_id: int, *, deleting: bool = False) -> bool:
    if user_id == recipe.owner_id:
        return True
    return deleting and access.store.role(user_id) == "owner"


@router.callback_query(F.data.startswith("recipe:new:"))
async def create_recipe_version(callback: CallbackQuery, state: FSMContext) -> None:
    token = (callback.data or "").removeprefix("recipe:new:")
    recipe = recipes.get(token)
    if not callback.from_user or not recipe or not _recipe_accessible(recipe, callback.from_user.id):
        await callback.answer("Рецепт недоступен или уже истёк.", show_alert=True)
        return
    await _reset_flow(state)
    await state.update_data(recipe=recipe, recipe_version=True)
    if recipe.kind == "sticker" and recipe.sticker_format:
        await _start_sticker_collection(callback.message, state, recipe.sticker_format)
        if callback.message:
            await callback.message.edit_text(
                "Отправьте исправленные исходники и нажмите /done. Если число файлов не изменится, emoji и название будут взяты из предыдущего пака."
            )
        await callback.answer()
        return
    await state.set_state(RenderFlow.waiting_for_source)
    if callback.message:
        await callback.message.edit_text(
            "Отправьте исправленный исходник для новой версии. Настройки сетки и emoji будут взяты из предыдущего пака."
        )
    await callback.answer()


@router.callback_query(F.data.startswith("recipe:delete:"))
async def confirm_recipe_delete(callback: CallbackQuery) -> None:
    token = (callback.data or "").removeprefix("recipe:delete:")
    recipe = recipes.get(token)
    if not callback.from_user or not recipe or not _recipe_accessible(recipe, callback.from_user.id, deleting=True):
        await callback.answer("Удаление недоступно или кнопка уже истекла.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да, удалить пак", callback_data=f"recipe:confirm:{token}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"recipe:cancel:{token}")],
        ]))
    await callback.answer("Подтвердите удаление пака.")


@router.callback_query(F.data.startswith("recipe:cancel:"))
async def cancel_recipe_delete(callback: CallbackQuery) -> None:
    await callback.answer("Удаление отменено.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("recipe:confirm:"))
async def delete_recipe_pack(callback: CallbackQuery, bot: Bot) -> None:
    token = (callback.data or "").removeprefix("recipe:confirm:")
    recipe = recipes.get(token)
    if not callback.from_user or not recipe or not _recipe_accessible(recipe, callback.from_user.id, deleting=True):
        await callback.answer("Удаление недоступно или кнопка уже истекла.", show_alert=True)
        return
    try:
        await bot.delete_sticker_set(recipe.pack_name)
    except TelegramAPIError as error:
        await callback.answer("Telegram не смог удалить пак.", show_alert=True)
        return
    recipes.discard(token)
    await activity_logger.event(bot, callback.from_user, "Пак", "удалён", recipe.pack_url)
    if callback.message:
        await callback.message.edit_text(f"Пак удалён: <code>{html.escape(recipe.pack_name)}</code>")
    await callback.answer("Пак удалён.")


async def _render_and_send_video_note(
    message: Message,
    state: FSMContext,
    source: Path,
    temporary: bool,
    **options,
) -> None:
    await state.update_data(
        video_note_source=str(source),
        video_note_temporary=temporary,
    )
    await message.answer("Готовлю кружок…", reply_markup=ReplyKeyboardRemove())
    result = None
    try:
        result = await _run_heavy_job(
            message, lambda: prepare_video_note(source, settings.temp_dir, **options)
        )
        if result.truncated:
            await message.answer(
                "Исходное видео длиннее 60 секунд — в кружок вошли первые 60 секунд."
            )
        await message.answer_video_note(
            video_note=FSInputFile(result.file),
            duration=max(1, round(result.duration)),
            length=640,
        )
    except (RenderError, JobAlreadyRunning, JobCancelled) as error:
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
    destination = settings.temp_dir / f"{message.from_user.id}_{video.file_unique_id}_{secrets.token_hex(6)}.mp4"
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
    await video_note_flow.start_options(message, state, destination, temporary=True)


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
    destination = settings.temp_dir / f"{message.from_user.id}_{document.file_unique_id}_{secrets.token_hex(6)}{suffix}"
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
    await video_note_flow.start_options(message, state, destination, temporary=True)


@router.message(RenderFlow.waiting_for_video_note_source, F.text)
async def receive_video_note_path(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    if not settings.allow_local_paths or message.from_user.id != settings.allowed_user_id:
        await message.answer("Отправьте исходник как файл через Telegram; локальные пути недоступны.")
        return
    source = parse_local_path(message.text or "")
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
    await video_note_flow.start_options(message, state, source, temporary=False)


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
    if not _fits_job_limit(_source_bytes(sources), document.file_size or 0):
        await _reject_oversized_job(message)
        return

    suffix = Path(document.file_name or "sticker").suffix
    destination = settings.temp_dir / f"{message.from_user.id}_{document.file_unique_id}_{secrets.token_hex(6)}{suffix}"
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
    if not settings.allow_local_paths or message.from_user.id != settings.allowed_user_id:
        await message.answer("Отправьте исходник как файл через Telegram; локальные пути недоступны.")
        return
    folder = parse_local_path(message.text or "")
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
    if not _fits_job_limit(_source_bytes(sources), sum(path.stat().st_size for path in accepted)):
        await _reject_oversized_job(message)
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


async def _suggest_name(message: Message, state: FSMContext, base: str | None = None) -> None:
    base = suggest_pack_name(base if base is not None else message.text or "")
    # Apply the existing Telegram name constraints before displaying the suggestion.
    me = await message.bot.get_me()
    if not me.username:
        await message.answer("У бота должен быть username. Укажите его в @BotFather.")
        return
    try:
        name = await available_pack_name(message.bot, base, me.username)
    except (TelegramAPIError, ValueError):
        name = make_sticker_set_name(base, me.username)
    await state.update_data(suggested_name=name)
    await _card(message, state,
        f"Предлагаю имя ссылки: <code>{html.escape(name)}</code>. Примите кнопкой или введите своё.",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=name)]], resize_keyboard=True, one_time_keyboard=True),
    )


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
    await _suggest_name(message, state)


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
        pack_name = await available_pack_name(bot, message.text or "", bot_user.username)
    except (ValueError, TelegramAPIError) as error:
        await message.answer(html.escape(str(error)))
        return

    requested_name = make_sticker_set_name(message.text or "", bot_user.username)
    if pack_name != requested_name:
        await message.answer(
            f"Имя занято. Предлагаю <code>{html.escape(pack_name)}</code>. Примите кнопкой или введите другое.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=pack_name)]], resize_keyboard=True),
        )
        return

    data: Dict[str, Any] = await state.get_data()
    sources = [Path(source) for source in data["sticker_sources"]]
    sticker_format = data["sticker_format"]
    await message.answer(f"Конвертирую {len(sources)} стикеров. Это может занять несколько минут…", reply_markup=ReplyKeyboardRemove())
    try:
        result = await _run_heavy_job(message, lambda: prepare_sticker_files(
            sources=sources, sticker_format=sticker_format, output_root=settings.temp_dir,
            fps=settings.default_fps, duration=settings.default_duration,
            max_static_size_kb=settings.max_static_sticker_size_kb,
            max_video_size_kb=settings.max_video_sticker_size_kb,
        ))
        await send_zip_parts(
            message, result.files, f"stickers_{sticker_format}", settings.temp_dir,
            f"Готово: {len(result.files)} файлов {sticker_format}."
        )
        status = await message.answer(f"Создаю sticker pack: загружено 0/{len(result.files)}…")

        async def report_progress(completed: int, total: int) -> None:
            if completed == total or completed == 1 or completed % 5 == 0:
                await status.edit_text(f"Создаю sticker pack: загружено {completed}/{total}…")

        async def report_retry(attempt: int, seconds: float) -> None:
            await status.edit_text(
                f"Telegram временно ограничил запросы. Повторяю через {seconds:g} с "
                f"(попытка {attempt}/4)…"
            )

        pack_url = await create_sticker_pack(
            bot=bot,
            user_id=message.from_user.id,
            files=result.files,
            title=data["sticker_pack_title"],
            name=pack_name,
            emojis=data["sticker_emojis"],
            sticker_format=sticker_format,
            progress=report_progress,
            retry_notice=report_retry,
        )
        recipe_token = secrets.token_urlsafe(8)
        recipes.put(Recipe(
            token=recipe_token, owner_id=message.from_user.id, pack_name=pack_name,
            pack_url=pack_url, kind="sticker", title=data["sticker_pack_title"],
            emoji=data["sticker_emojis"][0] if data["sticker_emojis"] else "🎨",
            sticker_format=sticker_format, emojis=tuple(data["sticker_emojis"]),
        ))
        await status.edit_text(
            f"Sticker pack создан: <a href=\"{pack_url}\">{html.escape(data['sticker_pack_title'])}</a>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить пак", url=pack_url)],
                [InlineKeyboardButton(text="🔁 Создать новую версию", callback_data=f"recipe:new:{recipe_token}")],
                [InlineKeyboardButton(text="🗑 Удалить пак", callback_data=f"recipe:delete:{recipe_token}")],
            ]),
        )
        await activity_logger.event(bot, message.from_user, "Sticker pack", "пак создан", pack_url)
    except (RenderError, JobAlreadyRunning, JobCancelled) as error:
        await activity_logger.event(bot, message.from_user, "Sticker pack", "ошибка конвертации")
        await message.answer(f"Конвертация не выполнена:\n<code>{html.escape(str(error)[:3500])}</code>")
    except TelegramAPIError as error:
        await activity_logger.event(bot, message.from_user, "Sticker pack", "ошибка Telegram")
        await message.answer(f"Telegram не смог создать пак:\n<code>{html.escape(str(error)[:3000])}</code>")
    except Exception as error:
        await activity_logger.event(bot, message.from_user, "Sticker pack", "непредвиденная ошибка")
        await message.answer(f"Не удалось завершить создание пака:\n<code>{html.escape(str(error)[:3000])}</code>")
    finally:
        if 'result' in locals() and result is not None:
            shutil.rmtree(result.directory, ignore_errors=True)
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
    destination = settings.temp_dir / f"{message.from_user.id}_{document.file_unique_id}_{secrets.token_hex(6)}{suffix}"
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
    destination = settings.temp_dir / f"{message.from_user.id}_{photo.file_unique_id}_{secrets.token_hex(6)}.jpg"
    await bot.download(photo, destination=destination)
    await _set_source(message, state, destination, temporary=True, source_kind="image")


@router.message(RenderFlow.waiting_for_source, F.text)
async def receive_path(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    if not settings.allow_local_paths or message.from_user.id != settings.allowed_user_id:
        await message.answer("Отправьте исходник как файл через Telegram; локальные пути недоступны.")
        return
    source = parse_local_path(message.text or "")
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
    selection = await confirmed_grid(message, state)
    if selection is None:
        return
    columns, rows = selection
    await state.update_data(columns=columns, rows=rows)
    await state.set_state(RenderFlow.waiting_for_pack_title)
    await _card(message, state,
        "Как будет называться пак? Это отображаемое название, например "
        f"<code>My art</code>. Я автоматически добавлю "
        f"<code>{html.escape(settings.pack_title_suffix)}</code>.",
        reply_markup=ReplyKeyboardRemove(),
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
        await _card(message, state, html.escape(str(error)))
        return
    await state.update_data(pack_title=title)
    await state.set_state(RenderFlow.waiting_for_pack_name)
    await _suggest_name(message, state)


@router.message(RenderFlow.waiting_for_pack_name, F.text)
async def receive_pack_name(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    bot_user = await bot.get_me()
    if not bot_user.username:
        await _card(message, state, "У бота должен быть username. Задайте его через @BotFather.")
        return
    try:
        pack_name = await available_pack_name(bot, message.text or "", bot_user.username)
    except (ValueError, TelegramAPIError) as error:
        await _card(message, state, html.escape(str(error)))
        return
    requested_name = make_sticker_set_name(message.text or "", bot_user.username)
    if pack_name != requested_name:
        await _card(message, state,
            f"Имя занято. Предлагаю <code>{html.escape(pack_name)}</code>. Примите кнопкой или введите другое.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=pack_name)]], resize_keyboard=True),
        )
        return

    await state.update_data(pack_name=pack_name)
    await state.set_state(RenderFlow.waiting_for_emoji)
    await _card(message, state,
        "Теперь отправьте <b>один эмодзи</b>. Он будет назначен всем video emoji "
        "в этом паке, например 🎨.",
        reply_markup=ReplyKeyboardRemove()
    )


@router.message(RenderFlow.waiting_for_emoji, F.text)
async def receive_emoji(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    try:
        emoji = validate_pack_emoji(message.text or "")
    except (ValueError, TelegramAPIError) as error:
        await message.answer(html.escape(str(error)))
        return

    data: Dict[str, Any] = await state.get_data()
    source = Path(data["source"])
    source_kind = data["source_kind"]
    columns, rows = int(data["columns"]), int(data["rows"])
    await _card(message, state, f"Рендерю сетку {columns}×{rows}. Это может занять несколько минут…")
    action_task = await _chat_action(message, "choose_sticker")
    try:
        if source_kind == "image":
            result = await _run_heavy_job(message, lambda: render_image_grid(
                source=source, columns=columns, rows=rows, output_root=settings.temp_dir,
            ))
        else:
            result = await _run_heavy_job(message, lambda: render_grid(
                source=source, columns=columns, rows=rows, output_root=settings.temp_dir,
                fps=settings.default_fps, duration=settings.default_duration,
                max_size_kb=settings.max_emoji_size_kb,
            ))
        await send_zip_parts(
            message, result.files, f"tg_art_{columns}x{rows}", settings.temp_dir,
            (
                f"Готово: {len(result.files)} файлов "
                f"{'PNG' if source_kind == 'image' else 'WebM'}.\n"
                f"Исходник: {result.source_info.width}×{result.source_info.height}, "
                f"{result.source_info.pixel_format}."
            ),
        )
        await _stop_chat_action(action_task)
        action_task = await _chat_action(message, "upload_document")
        await _card(message, state, f"Создаю emoji pack: загружено 0/{len(result.files)}…")
        last_reported = 0

        async def report_progress(completed: int, total: int) -> None:
            nonlocal last_reported
            if completed == total or completed - last_reported >= 5:
                last_reported = completed
                await _card(message, state, f"Создаю emoji pack: загружено {completed}/{total}…")

        async def report_retry(attempt: int, seconds: float) -> None:
            await _card(message, state,
                f"Telegram временно ограничил запросы. Повторяю через {seconds:g} с "
                f"(попытка {attempt}/4)…"
            )

        pack_url = await create_custom_emoji_pack(
            bot=bot,
            user_id=message.from_user.id,
            files=result.files,
            title=data["pack_title"],
            name=data["pack_name"],
            emoji=emoji,
            sticker_format="static" if source_kind == "image" else "video",
            progress=report_progress,
            retry_notice=report_retry,
        )
        preview_token = secrets.token_urlsafe(8)
        tg_art_previews.put(preview_token, {
            "user_id": message.from_user.id,
            "pack_name": data["pack_name"],
            "columns": columns,
            "rows": rows,
            "fallback_emoji": emoji,
        })
        recipe_token = secrets.token_urlsafe(8)
        recipes.put(Recipe(
            token=recipe_token, owner_id=message.from_user.id, pack_name=data["pack_name"],
            pack_url=pack_url, kind="tg_art", title=data["pack_title"], emoji=emoji,
            columns=columns, rows=rows,
        ))
        buttons = [
            [InlineKeyboardButton(text="➕ Добавить пак", url=pack_url)],
            [InlineKeyboardButton(text="🔁 Создать новую версию", callback_data=f"recipe:new:{recipe_token}")],
            [InlineKeyboardButton(text="🗑 Удалить пак", callback_data=f"recipe:delete:{recipe_token}")],
            [InlineKeyboardButton(text="🎨 Показать TG Art", callback_data=f"tg_art:{preview_token}")],
        ]
        await _card(message, state,
            f"Emoji pack создан: <a href=\"{pack_url}\">{html.escape(data['pack_title'])}</a>\n\n"
            "Добавьте пак в Telegram, затем нажмите «Показать TG Art».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        sticker_set = await bot.get_sticker_set(data["pack_name"])
        tg_art, entities = build_tg_art_grid(
            [item.custom_emoji_id for item in sticker_set.stickers if item.custom_emoji_id], columns, rows, emoji
        )
        await activity_logger.tg_art(bot, message.from_user, pack_url, tg_art, entities)
    except (RenderError, JobAlreadyRunning, JobCancelled) as error:
        await activity_logger.event(bot, message.from_user, "TG Art", "ошибка рендера")
        await message.answer(f"Рендер не выполнен:\n<code>{html.escape(str(error)[:3500])}</code>")
    except TelegramAPIError as error:
        await activity_logger.event(bot, message.from_user, "TG Art", "ошибка Telegram")
        await message.answer(
            "Telegram не смог создать пак. Если набор уже появился, часть emoji могла "
            "успеть загрузиться.\n\n"
            f"Ошибка: <code>{html.escape(str(error)[:3000])}</code>"
        )
    except Exception as error:
        await activity_logger.event(bot, message.from_user, "TG Art", "непредвиденная ошибка")
        await message.answer(
            "Не удалось завершить создание пака.\n\n"
            f"Ошибка: <code>{html.escape(str(error)[:3000])}</code>"
        )
    finally:
        await _stop_chat_action(action_task)
        if 'result' in locals() and result is not None:
            shutil.rmtree(result.directory, ignore_errors=True)
        if data.get("temporary"):
            source.unlink(missing_ok=True)
        await state.clear()


@router.message()
async def fallback(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await message.answer("Используйте /start, чтобы начать новый рендер, или /cancel для отмены.")


def _phase_hint(current):
    hints = {
        RenderFlow.waiting_for_grid.state: 'Ожидаю размер сетки.',
        RenderFlow.waiting_for_pack_title.state: 'Ожидаю название пака.',
        RenderFlow.waiting_for_sticker_pack_title.state: 'Ожидаю название стикерпака.',
        RenderFlow.waiting_for_pack_name.state: 'Ожидаю имя ссылки.',
        RenderFlow.waiting_for_sticker_pack_name.state: 'Ожидаю имя ссылки.',
        RenderFlow.waiting_for_emoji.state: 'Ожидаю emoji для пака.',
        RenderFlow.collecting_stickers.state: 'Ожидаю файлы для стикерпака.',
        'InboxFlow:sticker_kind': 'Ожидаю тип стикерпака для смешанного альбома.',
        'VideoNoteFlow:mode': 'Ожидаю способ масштабирования кружка.',
        'VideoNoteFlow:position': 'Ожидаю положение кадра.',
        'VideoNoteFlow:background': 'Ожидаю фон полей.',
        'VideoNoteFlow:hex_color': 'Ожидаю HEX цвета фона.',
        'AdminFlow:target': 'Ожидаю ID или контакт пользователя.',
    }
    return hints.get(current, 'Завершите текущий шаг или отправьте /cancel.')


def _resume_keyboard(current, data, actor):
    from app import download_flow
    from app.grid_keyboard import grid_keyboard
    if current == 'InboxFlow:choosing':
        return inbox_flow.action_keyboard(inbox_flow.stored_messages(data), actor)
    if current in (RenderFlow.waiting_for_grid.state, RenderFlow.waiting_for_existing_tg_art_grid.state):
        return grid_keyboard(*(data.get('pending_grid') or (0, 0)))
    if current in (RenderFlow.waiting_for_pack_name.state, RenderFlow.waiting_for_sticker_pack_name.state) and data.get('suggested_name'):
        return video_note_flow.keyboard([[data['suggested_name']]])
    return {
        RenderFlow.waiting_for_mode.state: MODE_KEYBOARD,
        RenderFlow.waiting_for_sticker_kind.state: STICKER_KIND_KEYBOARD,
        RenderFlow.collecting_stickers.state: inbox_flow.COLLECT_KEYBOARD,
        RenderFlow.waiting_for_sticker_emoji_mode.state: EMOJI_MODE_KEYBOARD,
        'InboxFlow:sticker_kind': STICKER_KIND_KEYBOARD,
        'VideoNoteFlow:mode': video_note_flow.MODE_KEYBOARD,
        'VideoNoteFlow:position': video_note_flow.POSITION_KEYBOARD,
        'VideoNoteFlow:background': video_note_flow.BACKGROUND_KEYBOARD,
        'VideoNoteFlow:hex_color': video_note_flow.HEX_KEYBOARD,
        'DownloadFlow:waiting_for_destination': download_flow.DESTINATION_KEYBOARD,
        'DownloadFlow:collecting': download_flow.COLLECT_KEYBOARD,
        'DownloadFlow:waiting_for_tgs_size': download_flow.SIZE_KEYBOARD,
        'DownloadFlow:waiting_for_tgs_color': download_flow.COLOR_KEYBOARD,
    }.get(current, ReplyKeyboardRemove())


def configure_interactions(value):
    video_note_flow.render = _render_and_send_video_note
    inbox_flow.config = SimpleNamespace(settings=value, reset=_reset_flow, set_source=_set_source,
        start_stickers=_start_sticker_collection, finish_stickers=_finish_sticker_collection,
        phase_hint=_phase_hint, resume_keyboard=_resume_keyboard)


async def main() -> None:
    global settings, heavy_jobs, recipes, activity_logger
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    heavy_jobs = HeavyJobQueue(settings.max_concurrent_jobs)
    recipes = RecipeCache(settings.recipe_ttl_seconds)
    activity_logger = ActivityLogger(settings.log_channel_id, settings.allowed_user_id)
    access.cancel_job = heavy_jobs.cancel
    cleanup_expired_job_directories(settings.temp_dir, settings.job_result_ttl_seconds)
    admin.reset_flow = _reset_flow
    admin.start_flow = start
    admin.refresh_commands = refresh_commands
    analysis_flow.reset_flow = _reset_flow
    analysis_flow.settings = settings
    access.store = access.AccessStore(settings.access_db, settings.allowed_user_id)
    configure_download_flow(settings)
    configure_interactions(settings)
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("FFmpeg и ffprobe не найдены в PATH")
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands(commands_for(settings.allowed_user_id))
    dispatcher = Dispatcher(storage=MemoryStorage())
    middleware = access.AccessMiddleware(settings.max_concurrent_jobs)
    dispatcher.message.outer_middleware(middleware)
    dispatcher.callback_query.outer_middleware(middleware)
    dispatcher.message.outer_middleware(AuditMiddleware(activity_logger))
    dispatcher.callback_query.outer_middleware(AuditMiddleware(activity_logger))
    dispatcher.message.outer_middleware(inbox_flow.ContentFirstMiddleware())
    dispatcher.include_router(inbox_flow.router)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(video_note_flow.router)
    dispatcher.include_router(analysis_flow.router)
    dispatcher.include_router(download_router)
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
