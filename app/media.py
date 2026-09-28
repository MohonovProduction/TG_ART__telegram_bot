from __future__ import annotations

from pathlib import Path
from typing import Optional


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".avif"}
VIDEO_EXTENSIONS = {".mov", ".mkv", ".webm", ".mp4", ".m4v", ".avi"}
ANIMATED_EXTENSIONS = {".tgs"}


def detect_source_kind(path: Path, mime_type: Optional[str] = None) -> str:
    mime = (mime_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if path.suffix.lower() in ANIMATED_EXTENSIONS:
        return "animated"
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return "image"
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return "video"
    raise ValueError("Поддерживаются изображения, видео и TGS-анимации")


def append_title_suffix(title: str, suffix: str) -> str:
    base = title.strip()
    normalized_suffix = suffix.strip()
    suffix_with_space = f" {normalized_suffix}" if normalized_suffix else ""
    result = base if suffix_with_space and base.lower().endswith(suffix_with_space.lower()) else f"{base}{suffix_with_space}"
    if not 1 <= len(result) <= 64:
        raise ValueError(
            f"Название вместе с «{normalized_suffix}» должно быть не длиннее 64 символов"
        )
    return result
