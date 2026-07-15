from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float
    pixel_format: str
    codec: str

    @property
    def has_alpha(self) -> bool:
        pixel_format = self.pixel_format.lower()
        return "a" in pixel_format or pixel_format in {"rgba", "argb", "bgra", "abgr"}


@dataclass(frozen=True)
class RenderResult:
    directory: Path
    archive: Path
    files: Sequence[Path]
    source_info: VideoInfo


def _parse_fps(value: str) -> float:
    try:
        numerator, denominator = value.split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


async def _run(command: Iterable[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RenderError(message[-3000:] or "FFmpeg завершился с ошибкой")
    return stdout.decode("utf-8", errors="replace")


async def probe_video(source: Path, ffprobe: str = "ffprobe") -> VideoInfo:
    output = await _run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,pix_fmt,codec_name,avg_frame_rate:format=duration",
            "-of", "json",
            str(source),
        ]
    )
    try:
        data = json.loads(output)
        stream = data["streams"][0]
        duration = float(data.get("format", {}).get("duration") or 0)
        return VideoInfo(
            width=int(stream["width"]),
            height=int(stream["height"]),
            duration=duration,
            fps=_parse_fps(stream.get("avg_frame_rate", "0/1")),
            pixel_format=stream.get("pix_fmt", "unknown"),
            codec=stream.get("codec_name", "unknown"),
        )
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RenderError("Не удалось прочитать видеопоток исходника") from error


def _tile_bounds(width: int, height: int, columns: int, rows: int, column: int, row: int):
    left = column * width // columns
    right = (column + 1) * width // columns
    top = row * height // rows
    bottom = (row + 1) * height // rows
    return left, top, right - left, bottom - top


async def _encode_tile(
    source: Path,
    destination: Path,
    crop: tuple,
    fps: int,
    duration: float,
    max_bytes: int,
    ffmpeg: str,
) -> None:
    left, top, width, height = crop
    attempts = (28, 32, 36, 40, 44, 48, 52)
    for crf in attempts:
        destination.unlink(missing_ok=True)
        video_filter = (
            f"crop={width}:{height}:{left}:{top},"
            "scale=100:100:flags=lanczos,"
            f"fps={fps},setsar=1,format=yuva420p"
        )
        await _run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-map", "0:v:0", "-an", "-t", str(duration),
                "-vf", video_filter,
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-b:v", "0", "-crf", str(crf),
                "-deadline", "good", "-cpu-used", "2", "-row-mt", "1",
                "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1",
                str(destination),
            ]
        )
        if destination.stat().st_size <= max_bytes:
            return
    raise RenderError(
        f"{destination.name} не удалось уменьшить до {max_bytes // 1024} KB. "
        "Попробуйте снизить FPS или упростить анимацию."
    )


async def render_grid(
    source: Path,
    columns: int,
    rows: int,
    output_root: Path,
    fps: int = 30,
    duration: float = 3.0,
    max_size_kb: int = 256,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> RenderResult:
    if not source.is_file():
        raise RenderError(f"Файл не найден: {source}")
    if not 1 <= columns <= 20 or not 1 <= rows <= 20:
        raise RenderError("Размер сетки должен быть от 1×1 до 20×20")
    if shutil.which(ffmpeg) is None or shutil.which(ffprobe) is None:
        raise RenderError("FFmpeg и ffprobe должны быть доступны в PATH")

    info = await probe_video(source, ffprobe)
    if info.width < columns or info.height < rows:
        raise RenderError("Сетка содержит больше ячеек, чем пикселей в исходнике")
    if not info.has_alpha:
        raise RenderError(
            f"Alpha-канал не найден (pix_fmt={info.pixel_format}). "
            "Экспортируйте исходник как ProRes 4444, FFV1 с alpha или PNG sequence."
        )

    output_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"{source.stem}_", dir=output_root))
    files: List[Path] = []
    effective_duration = min(duration, info.duration) if info.duration > 0 else duration
    try:
        for row in range(rows):
            for column in range(columns):
                destination = directory / f"tile_r{row + 1:02d}_c{column + 1:02d}.webm"
                crop = _tile_bounds(info.width, info.height, columns, rows, column, row)
                await _encode_tile(
                    source, destination, crop, min(30, fps), effective_duration,
                    max_size_kb * 1024, ffmpeg,
                )
                files.append(destination)

        archive = directory / f"{source.stem}_{columns}x{rows}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for file in files:
                zip_file.write(file, arcname=file.name)
        return RenderResult(directory, archive, files, info)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


async def render_image_grid(
    source: Path,
    columns: int,
    rows: int,
    output_root: Path,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> RenderResult:
    if not source.is_file():
        raise RenderError(f"Файл не найден: {source}")
    if not 1 <= columns <= 20 or not 1 <= rows <= 20:
        raise RenderError("Размер сетки должен быть от 1×1 до 20×20")
    if shutil.which(ffmpeg) is None or shutil.which(ffprobe) is None:
        raise RenderError("FFmpeg и ffprobe должны быть доступны в PATH")

    info = await probe_video(source, ffprobe)
    if info.width < columns or info.height < rows:
        raise RenderError("Сетка содержит больше ячеек, чем пикселей в исходнике")

    output_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"{source.stem}_", dir=output_root))
    files: List[Path] = []
    try:
        for row in range(rows):
            for column in range(columns):
                destination = directory / f"tile_r{row + 1:02d}_c{column + 1:02d}.png"
                left, top, width, height = _tile_bounds(
                    info.width, info.height, columns, rows, column, row
                )
                video_filter = (
                    f"crop={width}:{height}:{left}:{top},"
                    "scale=100:100:flags=lanczos,setsar=1"
                )
                await _run(
                    [
                        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(source), "-map", "0:v:0", "-frames:v", "1",
                        "-vf", video_filter, "-c:v", "png", str(destination),
                    ]
                )
                files.append(destination)

        archive = directory / f"{source.stem}_{columns}x{rows}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for file in files:
                zip_file.write(file, arcname=file.name)
        return RenderResult(directory, archive, files, info)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
