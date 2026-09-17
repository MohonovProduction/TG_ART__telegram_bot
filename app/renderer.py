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


@dataclass(frozen=True)
class VideoNoteResult:
    directory: Path
    file: Path
    source_info: VideoInfo
    duration: float
    truncated: bool


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


async def convert_webp_to_png(
    source: Path,
    destination: Path,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Convert a static Telegram sticker/custom emoji to transparent PNG."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    await _run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map", "0:v:0", "-frames:v", "1",
            "-vf", "format=rgba", "-c:v", "png", str(destination),
        ]
    )
    return destination


async def convert_webm_to_mov(
    source: Path,
    destination: Path,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> Path:
    """Convert a Telegram VP9 video sticker/custom emoji to ProRes 4444 MOV."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    await _run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-c:v", "libvpx-vp9", "-i", str(source),
            "-map", "0:v:0", "-an", "-c:v", "prores_ks",
            "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
            "-alpha_bits", "16", str(destination),
        ]
    )
    info = await probe_video(destination, ffprobe)
    if info.codec != "prores" or not info.has_alpha:
        destination.unlink(missing_ok=True)
        raise RenderError("Созданный MOV не содержит ожидаемый ProRes alpha-поток")
    return destination


def video_note_filter(length: int, mode: str, position: str, background: str) -> tuple[str, bool]:
    """Return a simple filter or a labelled complex graph for blurred Fit."""
    if mode not in ('cover', 'fit', 'fill') or position not in ('center', 'top', 'bottom', 'left', 'right'):
        raise RenderError('Некорректный режим масштабирования или положение кадра')
    if background != 'blur':
        import re
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', background):
            raise RenderError('Цвет фона должен быть в формате #RRGGBB')
    finish = 'fps=30,setsar=1,format=yuv420p'
    if mode == 'cover':
        x = '0' if position == 'left' else 'iw-min(iw\\,ih)' if position == 'right' else '(iw-min(iw\\,ih))/2'
        y = '0' if position == 'top' else 'ih-min(iw\\,ih)' if position == 'bottom' else '(ih-min(iw\\,ih))/2'
        return f"crop='min(iw\\,ih)':'min(iw\\,ih)':'{x}':'{y}',scale={length}:{length}:flags=lanczos,{finish}", False
    if mode == 'fill':
        return f'scale={length}:{length}:flags=lanczos,{finish}', False
    fit = f'scale={length}:{length}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,setsar=1'
    if background == 'blur':
        graph = (f'[0:v:0]split=2[bg][fg];[bg]scale={length}:{length}:force_original_aspect_ratio=increase:flags=lanczos,'
                 f"crop={length}:{length},setsar=1,boxblur=luma_radius='min(h,w)/20':luma_power=2[blur];"
                 f'[fg]{fit}[front];[blur][front]overlay=(W-w)/2:(H-h)/2,{finish}[v]')
        return graph, True
    return f'{fit},pad={length}:{length}:(ow-iw)/2:(oh-ih)/2:color=0x{background[1:]},{finish}', False


async def prepare_video_note(
    source: Path,
    output_root: Path,
    max_duration: float = 60.0,
    length: int = 640,
    max_size_mb: int = 47,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    mode: str = "cover",
    position: str = "center",
    background: str = "#000000",
) -> VideoNoteResult:
    """Convert a video to a square MPEG-4 file suitable for sendVideoNote."""
    if not source.is_file():
        raise RenderError(f"Файл не найден: {source}")
    if max_duration <= 0 or length < 2 or max_size_mb < 1:
        raise RenderError("Некорректные параметры кружка")
    if shutil.which(ffmpeg) is None or shutil.which(ffprobe) is None:
        raise RenderError("FFmpeg и ffprobe должны быть доступны в PATH")

    info = await probe_video(source, ffprobe)
    if info.width < 2 or info.height < 2:
        raise RenderError("Некорректный размер видеопотока")

    duration = min(max_duration, info.duration) if info.duration > 0 else max_duration
    truncated = info.duration > max_duration
    output_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="video_note_", dir=output_root))
    destination = directory / "video_note.mp4"
    max_bytes = max_size_mb * 1024 * 1024
    video_filter, complex_graph = video_note_filter(length, mode, position, background)

    try:
        for crf in (23, 27, 31, 35, 39):
            destination.unlink(missing_ok=True)
            await _run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source),
                    *( ["-filter_complex", video_filter, "-map", "[v]"] if complex_graph else ["-vf", video_filter, "-map", "0:v:0"] ),
                    "-map", "0:a:0?", "-t", str(duration),
                    "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                    "-profile:v", "main", "-level", "3.1",
                    "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                    "-movflags", "+faststart", str(destination),
                ]
            )
            if destination.stat().st_size <= max_bytes:
                return VideoNoteResult(
                    directory=directory,
                    file=destination,
                    source_info=info,
                    duration=duration,
                    truncated=truncated,
                )
        raise RenderError(
            f"Не удалось уменьшить кружок до лимита {max_size_mb} MB."
        )
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


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


async def _encode_sticker_video(
    source: Path,
    destination: Path,
    fps: int,
    duration: float,
    max_bytes: int,
    ffmpeg: str,
) -> None:
    """Convert an arbitrary source video into a Telegram video sticker.

    Keep the full 512 px canvas and requested frame rate whenever possible.  CRF is
    binary-searched so the first accepted result uses the lowest (best) CRF that
    fits.  Only files which cannot fit even at CRF 63 lose frame rate or visible
    content size.
    """
    requested_fps = min(30, fps)
    profiles = [(requested_fps, 512)]
    profiles.extend((fallback_fps, 512) for fallback_fps in (24, 20, 15, 12)
                    if fallback_fps < requested_fps)
    profiles.extend((requested_fps, side) for side in (480, 448, 416, 384, 352, 320))

    for profile_fps, content_size in profiles:
        video_filter = (
            f"scale={content_size}:{content_size}:force_original_aspect_ratio=decrease:flags=lanczos,"
            "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0,"
            f"fps={profile_fps},setsar=1,format=yuva420p"
        )
        # VP9's output size is monotonic enough in CRF for a binary search. This
        # avoids a long linear ladder while still choosing the best fitting CRF.
        low, high = 28, 63
        best_crf: Optional[int] = None
        while low <= high:
            crf = (low + high) // 2
            destination.unlink(missing_ok=True)
            await _run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source), "-map", "0:v:0", "-an", "-t", str(duration),
                    "-vf", video_filter, "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                    "-b:v", "0", "-crf", str(crf), "-deadline", "good", "-cpu-used", "2",
                    "-row-mt", "1", "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1",
                    str(destination),
                ]
            )
            if destination.stat().st_size <= max_bytes:
                best_crf = crf
                high = crf - 1
            else:
                low = crf + 1
        if best_crf is not None:
            # The last successful file may be from an earlier midpoint; encode the
            # exact best CRF so the output and the selected quality always match.
            destination.unlink(missing_ok=True)
            await _run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source), "-map", "0:v:0", "-an", "-t", str(duration),
                    "-vf", video_filter, "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                    "-b:v", "0", "-crf", str(best_crf), "-deadline", "good", "-cpu-used", "2",
                    "-row-mt", "1", "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1",
                    str(destination),
                ]
            )
            if destination.stat().st_size <= max_bytes:
                return
    raise RenderError(
        f"{source.name} не удалось уменьшить до {max_bytes // 1024} KB даже после "
        "снижения FPS и размера анимации."
    )


async def _encode_static_sticker(
    source: Path, destination: Path, max_bytes: int, ffmpeg: str
) -> None:
    video_filter = (
        "scale=512:512:force_original_aspect_ratio=decrease:flags=lanczos,"
        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0,format=rgba"
    )
    for quality in (85, 75, 65, 55, 45):
        destination.unlink(missing_ok=True)
        await _run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-map", "0:v:0", "-frames:v", "1", "-vf", video_filter,
                "-c:v", "libwebp", "-lossless", "0", "-q:v", str(quality), str(destination),
            ]
        )
        if destination.stat().st_size <= max_bytes:
            return
    raise RenderError(f"{source.name} не удалось уменьшить до {max_bytes // 1024} KB.")


async def prepare_sticker_files(
    sources: Sequence[Path],
    sticker_format: str,
    output_root: Path,
    fps: int = 30,
    duration: float = 3.0,
    max_static_size_kb: int = 512,
    max_video_size_kb: int = 256,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> RenderResult:
    """Convert source files to static WebP or video WebM Telegram stickers."""
    if sticker_format not in {"static", "video"}:
        raise RenderError("Формат стикера должен быть static или video")
    if not sources:
        raise RenderError("Нет файлов для создания набора")
    if max_static_size_kb < 1 or max_video_size_kb < 1:
        raise RenderError("Лимит размера стикера должен быть положительным")
    if shutil.which(ffmpeg) is None or shutil.which(ffprobe) is None:
        raise RenderError("FFmpeg и ffprobe должны быть доступны в PATH")

    output_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="stickers_", dir=output_root))
    files: List[Path] = []
    try:
        for index, source in enumerate(sources, start=1):
            info = await probe_video(source, ffprobe)
            if sticker_format == "static":
                destination = directory / f"sticker_{index:03d}.webp"
                await _encode_static_sticker(
                    source, destination, max_static_size_kb * 1024, ffmpeg
                )
            else:
                destination = directory / f"sticker_{index:03d}.webm"
                effective_duration = min(duration, info.duration) if info.duration > 0 else duration
                await _encode_sticker_video(
                    source, destination, fps, effective_duration, max_video_size_kb * 1024, ffmpeg
                )
            files.append(destination)

        archive = directory / f"stickers_{sticker_format}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for file in files:
                zip_file.write(file, arcname=file.name)
        return RenderResult(directory, archive, files, await probe_video(sources[0], ffprobe))
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


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
