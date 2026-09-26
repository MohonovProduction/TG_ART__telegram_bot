"""Create Telegram-sized ZIP parts and remove them after delivery."""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Sequence

from aiogram.types import FSInputFile, Message

MAX_DOCUMENT_BYTES = 49 * 1024 * 1024


def create_zip_parts(files: Sequence[Path], stem: str, temp_root: Path) -> tuple[Path, list[Path]]:
    """Build independently usable ZIP files that stay below Telegram's 50 MB limit."""
    directory = Path(tempfile.mkdtemp(prefix="archive_", dir=temp_root))
    groups: list[list[Path]] = [[]]
    estimated = 0
    for file in files:
        size = file.stat().st_size + 2048
        if groups[-1] and estimated + size > MAX_DOCUMENT_BYTES:
            groups.append([])
            estimated = 0
        groups[-1].append(file)
        estimated += size
    parts: list[Path] = []
    total = len(groups)
    for index, group in enumerate(groups, start=1):
        suffix = f"_part{index:02d}-of-{total:02d}" if total > 1 else ""
        archive = directory / f"{stem}{suffix}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for file in group:
                output.write(file, arcname=file.name)
        if archive.stat().st_size > MAX_DOCUMENT_BYTES:
            raise RuntimeError(f"Архив {archive.name} превышает безопасный лимит Telegram.")
        parts.append(archive)
    return directory, parts


async def send_zip_parts(message: Message, files: Sequence[Path], stem: str, temp_root: Path, caption: str) -> None:
    directory, parts = create_zip_parts(files, stem, temp_root)
    try:
        for index, archive in enumerate(parts, start=1):
            part_caption = caption if index == 1 else ""
            if len(parts) > 1:
                part_caption += ("\n" if part_caption else "") + f"Часть {index}/{len(parts)}."
            await message.answer_document(FSInputFile(archive), caption=part_caption)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
