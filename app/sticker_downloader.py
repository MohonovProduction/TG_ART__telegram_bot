from __future__ import annotations

import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


PACK_LINK_RE = re.compile(
    r"\s*(?:https?://)?t\.me/(addstickers|addemoji)/([A-Za-z][A-Za-z0-9_]{0,63})/?(?:\?[^\s#]*)?(?:#\S*)?\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StickerAsset:
    file_id: str
    file_unique_id: str
    emoji: str | None
    set_name: str | None
    format: str
    extension: str
    custom_emoji_id: str | None = None
    needs_repainting: bool = False
    source: str = "sticker"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StickerAsset":
        return cls(**data)


def asset_from_sticker(sticker: Any, source: str = "sticker") -> StickerAsset:
    if sticker.is_animated:
        sticker_format, extension = "animated", ".tgs"
    elif sticker.is_video:
        sticker_format, extension = "video", ".webm"
    else:
        sticker_format, extension = "static", ".webp"
    return StickerAsset(
        file_id=sticker.file_id,
        file_unique_id=sticker.file_unique_id,
        emoji=sticker.emoji,
        set_name=sticker.set_name,
        format=sticker_format,
        extension=extension,
        custom_emoji_id=sticker.custom_emoji_id,
        needs_repainting=bool(sticker.needs_repainting),
        source=source,
    )


def parse_pack_link(value: str) -> tuple[str, str]:
    match = PACK_LINK_RE.fullmatch(value)
    if not match:
        raise ValueError("Нужна ссылка t.me/addstickers/... или t.me/addemoji/...")
    return match.group(1).lower(), match.group(2)


def merge_assets(
    existing: Sequence[dict[str, Any]], incoming: Iterable[StickerAsset]
) -> tuple[list[dict[str, Any]], int]:
    result = list(existing)
    keys = {
        item.get("custom_emoji_id") or item.get("file_unique_id")
        for item in result
    }
    added = 0
    for asset in incoming:
        key = asset.custom_emoji_id or asset.file_unique_id
        if key in keys:
            continue
        keys.add(key)
        result.append(asset.to_dict())
        added += 1
    return result, added


def safe_asset_stem(asset: StickerAsset, index: int) -> str:
    kind = "emoji" if asset.custom_emoji_id else "sticker"
    identifier = re.sub(r"[^A-Za-z0-9_-]", "", asset.file_unique_id)[:32]
    return f"{index:03d}_{kind}_{identifier or 'file'}"


def unique_destination(directory: Path, filename: str) -> Path:
    """Return a non-existing destination without overwriting user files."""
    candidate = directory / filename
    counter = 2
    while candidate.exists():
        candidate = directory / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
        counter += 1
    return candidate


def move_completed_file(source: Path, directory: Path, filename: str) -> Path:
    destination = unique_destination(directory, filename)
    shutil.move(str(source), str(destination))
    return destination
