from __future__ import annotations

import html
import shutil
import tempfile
from pathlib import Path
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.config import Settings
from app import access
from app.path_utils import parse_local_path
from app.renderer import convert_webm_to_mov, convert_webp_to_png
from app.sticker_downloader import (
    StickerAsset,
    asset_from_sticker,
    merge_assets,
    move_completed_file,
    parse_pack_link,
    safe_asset_stem,
)
from app.tgs_renderer import normalize_hex_color, parse_render_size, render_tgs_to_mov
from app.telegram_retry import retry_telegram


class DownloadFlow(StatesGroup):
    waiting_for_destination = State()
    waiting_for_path = State()
    collecting = State()
    waiting_for_tgs_size = State()
    waiting_for_custom_size = State()
    waiting_for_tgs_color = State()


router = Router(name="sticker_download")
settings: Settings

DESTINATION_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📁 Папка по умолчанию")],
        [KeyboardButton(text="✏️ Указать путь")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)
COLLECT_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Сохранить"), KeyboardButton(text="🗑 Очистить")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
)
SIZE_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="×1"), KeyboardButton(text="×2"), KeyboardButton(text="×3"), KeyboardButton(text="×4")],
        [KeyboardButton(text="512"), KeyboardButton(text="1024"), KeyboardButton(text="2048")],
        [KeyboardButton(text="✏️ Свой размер"), KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)
COLOR_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⚪ #FFFFFF"), KeyboardButton(text="⚫ #000000")],
        [KeyboardButton(text="🔴 #FF0000"), KeyboardButton(text="🟢 #00FF00"), KeyboardButton(text="🔵 #0000FF")],
        [KeyboardButton(text="🎨 Исходные цвета"), KeyboardButton(text="✏️ Ввести HEX")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def configure(value: Settings) -> None:
    global settings
    settings = value


def _allowed(message: Message) -> bool:
    return bool(message.from_user and access.allowed(message.from_user.id))


async def _reject(message: Message) -> None:
    await message.answer("Этот локальный бот доступен только владельцу.")


async def _edit_progress(status: Message, text: str) -> bool:
    try:
        await status.edit_text(text)
        return True
    except TelegramAPIError:
        return False


async def start_download(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not settings.allow_local_paths or message.from_user.id != settings.allowed_user_id:
        await _begin_collection(message, state, settings.download_dir / str(message.from_user.id))
        return
    await state.set_state(DownloadFlow.waiting_for_destination)
    await message.answer(
        "Куда сохранить пачку? Файлы до 50 MB я также отправлю в этот чат.",
        reply_markup=DESTINATION_KEYBOARD,
    )


async def _begin_collection(message: Message, state: FSMContext, destination: Path) -> None:
    await state.update_data(download_root=str(destination), download_assets=[])
    await state.set_state(DownloadFlow.collecting)
    await message.answer(
        "Что можно сохранить:\n\n"
        "• Статичные стикеры и emoji — PNG.\n"
        "• Видео-стикеры и video emoji — MOV ProRes 4444 с alpha.\n"
        "• TGS-анимации — MOV ProRes 4444 с alpha.\n\n"
        "Присылайте стикеры, сообщения с Custom Emoji или ссылки на наборы "
        "<code>t.me/addstickers/...</code> / <code>t.me/addemoji/...</code>.\n\n"
        "Добавлено: <b>0</b>. Когда закончите, нажмите «Сохранить».",
        reply_markup=COLLECT_KEYBOARD,
    )


def _ensure_destination(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Указанный путь не является папкой")
    probe = path / ".tg_art_bot_write_test"
    try:
        probe.touch(exist_ok=False)
        probe.unlink()
    except OSError as error:
        raise ValueError("Нет доступа на запись в эту папку") from error
    return path


async def _add_assets(message: Message, state: FSMContext, assets: list[StickerAsset]) -> None:
    data = await state.get_data()
    merged, added = merge_assets(data.get("download_assets", []), assets)
    await state.update_data(download_assets=merged)
    counts = {kind: sum(item["format"] == kind for item in merged) for kind in ("static", "animated", "video")}
    duplicate_note = " Дубликаты пропущены." if added < len(assets) else ""
    await message.answer(
        f"Добавлено: <b>{added}</b>. Всего: <b>{len(merged)}</b> "
        f"(WebP: {counts['static']}, TGS: {counts['animated']}, WebM: {counts['video']})."
        f"{duplicate_note}",
        reply_markup=COLLECT_KEYBOARD,
    )


@router.message(Command("download"))
@router.message(Command("save_stickers"))
async def download_command(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await start_download(message, state)


@router.message(DownloadFlow.waiting_for_destination, F.text)
async def receive_destination(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    if not settings.allow_local_paths or message.from_user.id != settings.allowed_user_id:
        await _begin_collection(message, state, settings.download_dir / str(message.from_user.id))
        return
    value = (message.text or "").strip().lower()
    if value == "❌ отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    elif value == "📁 папка по умолчанию":
        await _begin_collection(message, state, settings.download_dir)
    elif value == "✏️ указать путь":
        await state.set_state(DownloadFlow.waiting_for_path)
        await message.answer(
            "Пришлите путь к папке. Поддерживаются пути из Finder в одинарных или двойных кавычках, например:\n"
            "<code>'/Users/me/Documents/Sticker exports'</code>",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await message.answer("Выберите папку кнопкой.", reply_markup=DESTINATION_KEYBOARD)


@router.message(DownloadFlow.waiting_for_path, F.text)
async def receive_destination_path(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    if not settings.allow_local_paths or message.from_user.id != settings.allowed_user_id:
        await message.answer("Выбор локальной папки недоступен.")
        return
    try:
        destination = _ensure_destination(parse_local_path(message.text or ""))
    except (OSError, ValueError) as error:
        await message.answer(f"Не удалось использовать папку: {html.escape(str(error))}")
        return
    await _begin_collection(message, state, destination)


@router.message(DownloadFlow.collecting, F.text == "❌ Отмена")
async def cancel_download(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(DownloadFlow.collecting, F.text == "🗑 Очистить")
async def clear_download_collection(message: Message, state: FSMContext) -> None:
    await state.update_data(download_assets=[])
    await message.answer("Список очищен. Добавлено: <b>0</b>.", reply_markup=COLLECT_KEYBOARD)


@router.message(DownloadFlow.collecting, F.sticker)
async def collect_sticker(message: Message, state: FSMContext) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    assert message.sticker is not None
    await _add_assets(message, state, [asset_from_sticker(message.sticker)])


@router.message(DownloadFlow.collecting, F.text == "✅ Сохранить")
async def finish_collection(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    assets = [StickerAsset.from_dict(item) for item in data.get("download_assets", [])]
    if not assets:
        await message.answer("Сначала добавьте хотя бы один стикер или Custom Emoji.")
        return
    if any(asset.format == "animated" for asset in assets):
        await state.set_state(DownloadFlow.waiting_for_tgs_size)
        await message.answer("Выберите размер MOV для TGS-анимаций.", reply_markup=SIZE_KEYBOARD)
    else:
        await _save_batch(message, state, bot, size=None, color=None)


@router.message(DownloadFlow.collecting, F.text)
async def collect_text_assets(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    custom_ids = list(dict.fromkeys(
        entity.custom_emoji_id
        for entity in (message.entities or [])
        if entity.type == "custom_emoji" and entity.custom_emoji_id
    ))
    try:
        if custom_ids:
            stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=custom_ids)
            await _add_assets(message, state, [asset_from_sticker(item, "custom_emoji") for item in stickers])
            return
        _, pack_name = parse_pack_link(message.text or "")
        sticker_set = await bot.get_sticker_set(pack_name)
        await _add_assets(
            message,
            state,
            [asset_from_sticker(item, f"pack:{pack_name}") for item in sticker_set.stickers],
        )
    except ValueError as error:
        await message.answer(html.escape(str(error)), reply_markup=COLLECT_KEYBOARD)
    except TelegramAPIError as error:
        await message.answer(f"Telegram не смог получить набор: <code>{html.escape(str(error)[:2000])}</code>")


@router.message(DownloadFlow.waiting_for_tgs_size, F.text)
async def receive_tgs_size(message: Message, state: FSMContext, bot: Bot) -> None:
    value = (message.text or "").strip()
    if value == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    if value == "✏️ Свой размер":
        await state.set_state(DownloadFlow.waiting_for_custom_size)
        await message.answer("Введите размер стороны от 64 до 4096 px.", reply_markup=ReplyKeyboardRemove())
        return
    try:
        size = parse_render_size(value)
    except ValueError as error:
        await message.answer(html.escape(str(error)), reply_markup=SIZE_KEYBOARD)
        return
    await _continue_after_size(message, state, bot, size)


@router.message(DownloadFlow.waiting_for_custom_size, F.text)
async def receive_custom_size(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        size = parse_render_size(message.text or "")
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    await _continue_after_size(message, state, bot, size)


async def _continue_after_size(message: Message, state: FSMContext, bot: Bot, size: int) -> None:
    await state.update_data(tgs_size=size)
    data = await state.get_data()
    assets = [StickerAsset.from_dict(item) for item in data["download_assets"]]
    if any(asset.format == "animated" and asset.needs_repainting for asset in assets):
        await state.set_state(DownloadFlow.waiting_for_tgs_color)
        await message.answer(
            "В пачке есть перекрашиваемые TGS. Выберите общий цвет; по умолчанию — белый.",
            reply_markup=COLOR_KEYBOARD,
        )
        return
    await _save_batch(message, state, bot, size, None)


@router.message(DownloadFlow.waiting_for_tgs_color, F.text)
async def receive_tgs_color(message: Message, state: FSMContext, bot: Bot) -> None:
    value = (message.text or "").strip()
    if value == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    if value == "✏️ Ввести HEX":
        await message.answer("Введите HEX, например <code>#72E6A6</code>.", reply_markup=ReplyKeyboardRemove())
        return
    color = None
    if value != "🎨 Исходные цвета":
        candidate = value.split()[-1]
        try:
            color = normalize_hex_color(candidate)
        except ValueError as error:
            await message.answer(html.escape(str(error)), reply_markup=COLOR_KEYBOARD)
            return
    data = await state.get_data()
    await _save_batch(message, state, bot, int(data["tgs_size"]), color)


async def _save_batch(
    message: Message,
    state: FSMContext,
    bot: Bot,
    size: int | None,
    color: str | None,
) -> None:
    data = await state.get_data()
    assets = [StickerAsset.from_dict(item) for item in data["download_assets"]]
    destination_root = Path(data["download_root"])
    actor_id = getattr(getattr(message, "from_user", None), "id", settings.allowed_user_id)
    save_locally = settings.allow_local_paths and actor_id == settings.allowed_user_id
    job_dir = Path(tempfile.mkdtemp(prefix="sticker_download_", dir=settings.temp_dir))
    status = await message.answer(
        f"Сохраняю стикеры: готово <b>0/{len(assets)}</b>…",
        reply_markup=ReplyKeyboardRemove(),
    )
    deliverables: list[Path] = []
    failures: list[str] = []
    try:
        source_dir = job_dir / "sources"
        result_dir = job_dir / "results"
        source_dir.mkdir()
        result_dir.mkdir()
        for index, asset in enumerate(assets, start=1):
            stem = safe_asset_stem(asset, index)
            if index == 1 or (index - 1) % 5 == 0:
                await _edit_progress(status,
                    f"Сохраняю стикеры: готово <b>{index - 1}/{len(assets)}</b>…\n"
                    f"Обрабатываю файл <b>{index}</b>."
                )
            source = source_dir / f"{stem}{asset.extension}"
            try:
                await retry_telegram(lambda: bot.download(asset.file_id, destination=source))
                if asset.format == "static":
                    temporary_result = result_dir / f"{stem}.png"
                    final_name = f"{stem}.png"
                    await convert_webp_to_png(source, temporary_result)
                elif asset.format == "video":
                    temporary_result = result_dir / f"{stem}.mov"
                    final_name = f"{stem}.mov"
                    await convert_webm_to_mov(source, temporary_result)
                else:
                    if size is None:
                        raise ValueError("Для TGS не выбран размер MOV")
                    applied_color = color if asset.needs_repainting else None
                    color_suffix = f"_{applied_color[1:]}" if applied_color else ""
                    final_name = f"{stem}_{size}px{color_suffix}.mov"
                    temporary_result = result_dir / final_name
                    await render_tgs_to_mov(
                        source,
                        temporary_result,
                        size=size,
                        color=applied_color,
                    )
                if save_locally:
                    deliverables.append(move_completed_file(temporary_result, destination_root, final_name))
                else:
                    # Cloud runs have no meaningful user-visible local folder. Keep the
                    # result only for the current delivery and remove it in finally.
                    deliverables.append(temporary_result)
            except Exception as error:
                failures.append(f"{index}: {type(error).__name__}: {str(error)[:500]}")

        await _edit_progress(status,
            f"Сохраняю стикеры: готово <b>{len(deliverables)}/{len(assets)}</b>.\n"
            "Отправляю файлы в чат…"
        )
        sent = 0
        oversized = 0
        for index, path in enumerate(deliverables, start=1):
            if path.stat().st_size > 50 * 1024 * 1024:
                oversized += 1
                continue
            try:
                await message.answer_document(FSInputFile(path))
                sent += 1
            except Exception:
                failures.append(f"Не отправлен в чат: {path.name}")
            if index == len(deliverables) or index % 5 == 0:
                await _edit_progress(status,
                    f"Сохраняю стикеры: готово <b>{len(deliverables)}/{len(assets)}</b>.\n"
                    f"Отправлено в чат: <b>{sent}/{len(deliverables)}</b>…"
                )

        png_count = sum(path.suffix.lower() == ".png" for path in deliverables)
        mov_count = sum(path.suffix.lower() == ".mov" for path in deliverables)
        location = (
            f"Папка: <code>{html.escape(str(destination_root))}</code>\n"
            if save_locally
            else "Файлы отправлены в чат и удалены с сервера.\n"
        )
        summary = (
            f"Готово: <b>{len(deliverables)}/{len(assets)}</b>.\n"
            f"PNG: <b>{png_count}</b>, MOV: <b>{mov_count}</b>.\n"
            + location
            + f"Отправлено в чат: <b>{sent}</b>."
        )
        if oversized:
            summary += f"\nБольше 50 MB, только локально: <b>{oversized}</b>."
        if failures:
            summary += f"\nОшибок: <b>{len(failures)}</b>."
            summary += "\n<code>" + html.escape("\n".join(failures[:3])) + "</code>"
        if not await _edit_progress(status, summary):
            await message.answer(summary)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        await state.clear()
