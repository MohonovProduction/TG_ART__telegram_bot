from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Sequence

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InputSticker


ProgressCallback = Callable[[int, int], Awaitable[None]]


def make_sticker_set_name(value: str, bot_username: str) -> str:
    """Build a Bot API-compatible set name and append its required suffix."""
    username = bot_username.lstrip("@").lower()
    suffix = f"_by_{username}"
    base = value.strip().lower().replace("-", "_").replace(" ", "_")
    base = re.sub(r"[^a-z0-9_]", "", base)
    base = re.sub(r"_+", "_", base).strip("_")
    if base.endswith(suffix):
        base = base[: -len(suffix)].rstrip("_")
    if not base or not base[0].isalpha():
        raise ValueError("Короткое имя должно начинаться с английской буквы")
    max_base_length = 64 - len(suffix)
    if max_base_length < 1:
        raise ValueError("Username бота слишком длинный для имени набора")
    base = base[:max_base_length].rstrip("_")
    if not base:
        raise ValueError("Короткое имя получилось пустым")
    return f"{base}{suffix}"


def validate_pack_title(value: str) -> str:
    title = value.strip()
    if not 1 <= len(title) <= 64:
        raise ValueError("Название пака должно содержать от 1 до 64 символов")
    return title


def validate_pack_emoji(value: str) -> str:
    emoji = value.strip()
    if not emoji or any(character.isspace() for character in emoji):
        raise ValueError("Отправьте один эмодзи без пробелов")
    if len(emoji) > 32:
        raise ValueError("Отправьте один эмодзи, а не строку текста")
    if emoji.isascii() and emoji.isalnum():
        raise ValueError("Нужен эмодзи, например 🎨")
    return emoji


async def _upload_sticker(bot: "Bot", user_id: int, path: Path, emoji: str) -> "InputSticker":
    from aiogram.types import FSInputFile, InputSticker

    uploaded = await bot.upload_sticker_file(
        user_id=user_id,
        sticker=FSInputFile(path),
        sticker_format="video",
        request_timeout=120,
    )
    if not uploaded.file_id:
        raise RuntimeError(f"Telegram не вернул file_id для {path.name}")
    return InputSticker(sticker=uploaded.file_id, format="video", emoji_list=[emoji])


async def create_custom_emoji_pack(
    bot: "Bot",
    user_id: int,
    files: Sequence[Path],
    title: str,
    name: str,
    emoji: str,
    progress: Optional[ProgressCallback] = None,
) -> str:
    if not files:
        raise ValueError("Нет файлов для создания пака")
    if len(files) > 200:
        raise ValueError("В одном custom emoji pack может быть не больше 200 элементов")

    total = len(files)
    first = await _upload_sticker(bot, user_id, files[0], emoji)
    await bot.create_new_sticker_set(
        user_id=user_id,
        name=name,
        title=title,
        stickers=[first],
        sticker_type="custom_emoji",
        request_timeout=120,
    )
    if progress:
        await progress(1, total)

    for index, path in enumerate(files[1:], start=2):
        sticker = await _upload_sticker(bot, user_id, path, emoji)
        await bot.add_sticker_to_set(
            user_id=user_id,
            name=name,
            sticker=sticker,
            request_timeout=120,
        )
        if progress:
            await progress(index, total)

    return f"https://t.me/addemoji/{name}"
