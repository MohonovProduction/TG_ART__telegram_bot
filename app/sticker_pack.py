from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Sequence, Union

from aiogram.types import MessageEntity
from app.telegram_retry import retry_telegram
from app.telegram_retry import RetryNotice

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InputSticker


ProgressCallback = Callable[[int, int], Awaitable[None]]
logger = logging.getLogger(__name__)


def parse_custom_emoji_pack_name(value: str) -> str:
    """Extract a custom emoji pack name from its Telegram addemoji link."""
    match = re.fullmatch(
        r"\s*(?:https?://)?t\.me/addemoji/([A-Za-z][A-Za-z0-9_]{0,63})/?(?:\?[^\s#]*)?(?:#\S*)?\s*",
        value,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("Пришлите ссылку вида https://t.me/addemoji/pack_name")
    return match.group(1)


def build_tg_art_grid(
    custom_emoji_ids: Sequence[str],
    columns: int,
    rows: int,
    fallback_emojis: Union[str, Sequence[str]],
) -> tuple[str, list[MessageEntity]]:
    """Build a row-major custom-emoji message matching the rendered grid.

    Telegram requires each custom-emoji entity to wrap its own regular emoji fallback.
    """
    if (
        columns < 1
        or rows < 1
        or len(custom_emoji_ids) != columns * rows
    ):
        raise ValueError("Число custom emoji не соответствует размеру сетки")

    emoji_fallbacks = (
        [fallback_emojis] * len(custom_emoji_ids)
        if isinstance(fallback_emojis, str)
        else list(fallback_emojis)
    )
    if len(emoji_fallbacks) != len(custom_emoji_ids) or any(not emoji for emoji in emoji_fallbacks):
        raise ValueError("Для каждого custom emoji нужен fallback-эмодзи")

    text_parts: list[str] = []
    entities: list[MessageEntity] = []
    offset = 0
    for index, (custom_emoji_id, fallback_emoji) in enumerate(
        zip(custom_emoji_ids, emoji_fallbacks)
    ):
        text_parts.append(fallback_emoji)
        emoji_length = len(fallback_emoji.encode("utf-16-le")) // 2
        entities.append(
            MessageEntity(
                type="custom_emoji",
                offset=offset,
                length=emoji_length,
                custom_emoji_id=custom_emoji_id,
            )
        )
        offset += emoji_length
        if (index + 1) % columns == 0 and index + 1 < len(custom_emoji_ids):
            text_parts.append("\n")
            offset += 1
    return "".join(text_parts), entities


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


def _is_emoji_base(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or codepoint in {0x00A9, 0x00AE, 0x2122, 0x3030, 0x303D, 0x3297, 0x3299}
    )


def _consume_emoji_tail(value: str, index: int) -> int:
    """Consume variation selectors, modifiers, keycaps and tag characters."""
    while index < len(value):
        codepoint = ord(value[index])
        if value[index] in {"\ufe0e", "\ufe0f", "\u20e3"} or unicodedata.combining(value[index]):
            index += 1
        elif 0x1F3FB <= codepoint <= 0x1F3FF or 0xE0020 <= codepoint <= 0xE007F:
            index += 1
        else:
            break
    return index


def split_pack_emojis(value: str) -> list[str]:
    """Split a Unicode emoji sequence, accepting spaces and line breaks as separators."""
    emojis: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character.isspace():
            index += 1
            continue

        is_keycap = character in "#*0123456789" and (
            index + 1 < len(value)
            and value[index + 1] in {"\ufe0e", "\ufe0f", "\u20e3"}
        )
        if not _is_emoji_base(character) and not is_keycap:
            raise ValueError("В списке должны быть только emoji, разделённые пробелами или переносами строк")

        start = index
        regional_indicator = 0x1F1E6 <= ord(character) <= 0x1F1FF
        index = _consume_emoji_tail(value, index + 1)
        if regional_indicator and index < len(value) and 0x1F1E6 <= ord(value[index]) <= 0x1F1FF:
            index = _consume_emoji_tail(value, index + 1)

        while index < len(value) and value[index] == "\u200d":
            if index + 1 >= len(value) or not _is_emoji_base(value[index + 1]):
                raise ValueError("Не удалось распознать emoji после символа соединения")
            index = _consume_emoji_tail(value, index + 2)
        emojis.append(value[start:index])

    if not emojis:
        raise ValueError("Отправьте хотя бы один emoji")
    return emojis


def validate_pack_emoji(value: str) -> str:
    emojis = split_pack_emojis(value)
    if len(emojis) != 1:
        raise ValueError("Отправьте один emoji")
    return emojis[0]


async def _upload_sticker(
    bot: "Bot", user_id: int, path: Path, emoji: str, sticker_format: str,
    retry_notice: RetryNotice | None = None,
) -> "InputSticker":
    from aiogram.types import FSInputFile, InputSticker

    uploaded = await retry_telegram(lambda: bot.upload_sticker_file(
        user_id=user_id, sticker=FSInputFile(path), sticker_format=sticker_format,
        request_timeout=120,
    ), notice=retry_notice)
    if not uploaded.file_id:
        raise RuntimeError(f"Telegram не вернул file_id для {path.name}")
    return InputSticker(sticker=uploaded.file_id, format=sticker_format, emoji_list=[emoji])


async def create_custom_emoji_pack(
    bot: "Bot",
    user_id: int,
    files: Sequence[Path],
    title: str,
    name: str,
    emojis: Sequence[str] | str,
    sticker_format: str = "video",
    progress: Optional[ProgressCallback] = None,
    retry_notice: RetryNotice | None = None,
) -> str:
    return await create_sticker_pack(
        bot=bot,
        user_id=user_id,
        files=files,
        title=title,
        name=name,
        emojis=[emojis] * len(files) if isinstance(emojis, str) else emojis,
        sticker_format=sticker_format,
        sticker_type="custom_emoji",
        progress=progress,
        retry_notice=retry_notice,
    )


async def create_sticker_pack(
    bot: "Bot",
    user_id: int,
    files: Sequence[Path],
    title: str,
    name: str,
    emojis: Sequence[str],
    sticker_format: str,
    sticker_type: str = "regular",
    progress: Optional[ProgressCallback] = None,
    retry_notice: RetryNotice | None = None,
) -> str:
    if not files:
        raise ValueError("Нет файлов для создания пака")
    if sticker_format not in {"static", "animated", "video"}:
        raise ValueError("Формат пака должен быть static, animated или video")
    if sticker_type not in {"regular", "custom_emoji"}:
        raise ValueError("Тип пака должен быть regular или custom_emoji")
    if len(files) != len(emojis):
        raise ValueError("Для каждого файла должен быть указан эмодзи")

    max_stickers = 200 if sticker_type == "custom_emoji" else (50 if sticker_format == "video" else 120)
    if len(files) > max_stickers:
        raise ValueError(f"В таком наборе может быть не больше {max_stickers} стикеров")

    total = len(files)

    first = await _upload_sticker(bot, user_id, files[0], emojis[0], sticker_format, retry_notice)
    await bot.create_new_sticker_set(
        user_id=user_id,
        name=name,
        title=title,
        stickers=[first],
        sticker_type=sticker_type,
        request_timeout=120,
    )
    if progress:
        await progress(1, total)

    for index, path in enumerate(files[1:], start=2):
        logger.info("Uploading sticker %s/%s (format=%s)", index, total, sticker_format)
        sticker = await _upload_sticker(bot, user_id, path, emojis[index - 1], sticker_format, retry_notice)
        await retry_telegram(lambda: bot.add_sticker_to_set(
            user_id=user_id, name=name, sticker=sticker, request_timeout=120,
        ), notice=retry_notice)
        if progress:
            await progress(index, total)

    link_type = "addemoji" if sticker_type == "custom_emoji" else "addstickers"
    return f"https://t.me/{link_type}/{name}"


def suggest_pack_name(title: str) -> str:
    """Transliterate a display title without relying on an external service."""
    letters = 'абвгдеёжзийклмнопрстуфхцчшщъыьэюя'
    replacements = ['a','b','v','g','d','e','yo','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya']
    table = dict(zip(letters, replacements))
    text = ''.join(table.get(char, char) for char in title.lower())
    base = re.sub(r'[^a-z0-9]+', '_', text).strip('_') or 'art'
    if not base[0].isalpha():
        base = 'art_' + base
    return base
