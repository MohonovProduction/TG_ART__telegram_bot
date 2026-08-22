from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.renderer import RenderError, probe_video


HEX_COLOR_RE = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")


def normalize_hex_color(value: str) -> str:
    match = HEX_COLOR_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("Цвет должен быть в формате #RRGGBB, например #72E6A6")
    color = match.group(1).upper()
    if len(color) == 3:
        color = "".join(character * 2 for character in color)
    return f"#{color}"


def parse_render_size(value: str) -> int:
    text = value.strip().lower().replace("×", "x")
    multipliers = {"x1": 512, "x2": 1024, "x3": 1536, "x4": 2048}
    if text in multipliers:
        return multipliers[text]
    try:
        size = int(text)
    except ValueError as error:
        raise ValueError("Введите размер от 64 до 4096 px") from error
    if not 64 <= size <= 4096:
        raise ValueError("Размер должен быть от 64 до 4096 px")
    return size


async def render_tgs_to_mov(
    source: Path,
    destination: Path,
    size: int,
    color: str | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> Path:
    """Render vector TGS frames directly at target size and encode ProRes 4444."""
    if not source.is_file():
        raise RenderError(f"TGS не найден: {source}")
    if not 64 <= size <= 4096:
        raise RenderError("Размер TGS должен быть от 64 до 4096 px")
    try:
        from rlottie_python import LottieAnimation
    except ImportError as error:
        raise RenderError(
            "Не установлен rlottie-python. Выполните pip install -r requirements.txt"
        ) from error

    try:
        animation = LottieAnimation.from_tgs(str(source))
        fps = float(animation.lottie_animation_get_framerate())
        frames = int(animation.lottie_animation_get_totalframe())
    except Exception as error:
        raise RenderError("Не удалось прочитать TGS-анимацию") from error
    if fps <= 0 or frames <= 0:
        raise RenderError("В TGS отсутствуют кадры или FPS")

    destination.parent.mkdir(parents=True, exist_ok=True)
    video_filter = "unpremultiply=inplace=1"
    if color is not None:
        normalized = normalize_hex_color(color)
        red = int(normalized[1:3], 16)
        green = int(normalized[3:5], 16)
        blue = int(normalized[5:7], 16)
        video_filter += f",lutrgb=r={red}:g={green}:b={blue}"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pixel_format", "bgra",
        "-video_size", f"{size}x{size}", "-framerate", f"{fps:g}",
        "-i", "pipe:0", "-an", "-vf", video_filter,
        "-c:v", "prores_ks", "-profile:v", "4444",
        "-pix_fmt", "yuva444p10le", "-alpha_bits", "16",
        str(destination),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    try:
        for frame_number in range(frames):
            frame = animation.lottie_animation_render(
                frame_num=frame_number,
                width=size,
                height=size,
            )
            process.stdin.write(frame)
            await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
        stderr = await process.stderr.read() if process.stderr else b""
        return_code = await process.wait()
    except Exception:
        if process.returncode is None:
            process.kill()
        await process.wait()
        destination.unlink(missing_ok=True)
        raise
    if return_code:
        destination.unlink(missing_ok=True)
        detail = stderr.decode("utf-8", errors="replace")[-3000:]
        raise RenderError(detail or "FFmpeg не смог создать MOV")

    info = await probe_video(destination, ffprobe)
    if info.codec != "prores" or "a" not in info.pixel_format:
        destination.unlink(missing_ok=True)
        raise RenderError("Созданный MOV не содержит ожидаемый ProRes alpha-поток")
    return destination
