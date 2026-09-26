from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_user_id: int
    output_dir: Path
    temp_dir: Path
    download_dir: Path
    access_db: Path = PROJECT_ROOT / "data/access.sqlite3"
    allow_local_paths: bool = True
    max_concurrent_jobs: int = 2
    default_fps: int = 30
    default_duration: float = 3.0
    max_emoji_size_kb: int = 256
    max_static_sticker_size_kb: int = 512
    max_video_sticker_size_kb: int = 256
    max_job_input_mb: int = 500
    job_result_ttl_seconds: int = 3600
    pack_title_suffix: str = "by @mohonovproduction"
    log_channel_id: int | None = None
    recipe_ttl_seconds: int = 3600

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        user_id = os.getenv("ALLOWED_USER_ID", "").strip()
        if not token or token == "put_your_bot_token_here":
            raise RuntimeError("Укажите BOT_TOKEN в файле .env")
        if not user_id.isdigit():
            raise RuntimeError("Укажите числовой ALLOWED_USER_ID в файле .env")

        def project_path(variable: str, default: str) -> Path:
            path = Path(os.getenv(variable, default)).expanduser()
            return path if path.is_absolute() else PROJECT_ROOT / path

        def positive_int(variable: str, default: str, minimum: int = 1) -> int:
            try:
                value = int(os.getenv(variable, default))
            except ValueError as error:
                raise RuntimeError(f"{variable} должен быть целым числом") from error
            if value < minimum:
                raise RuntimeError(f"{variable} должен быть не меньше {minimum}")
            return value

        settings = cls(
            access_db=project_path("ACCESS_DB", "./data/access.sqlite3").resolve(),
            allow_local_paths=os.getenv("ALLOW_LOCAL_PATHS", "true").lower() == "true",
            max_concurrent_jobs=positive_int("MAX_CONCURRENT_JOBS", "2"),
            bot_token=token,
            allowed_user_id=int(user_id),
            output_dir=project_path("OUTPUT_DIR", "./output").resolve(),
            temp_dir=project_path("TEMP_DIR", "./temp").resolve(),
            download_dir=project_path("DOWNLOAD_DIR", "./downloads").resolve(),
            default_fps=min(30, max(1, int(os.getenv("DEFAULT_FPS", "30")))),
            default_duration=min(3.0, max(0.1, float(os.getenv("DEFAULT_DURATION", "3")))),
            max_emoji_size_kb=positive_int("MAX_EMOJI_SIZE_KB", "256"),
            max_static_sticker_size_kb=positive_int("MAX_STATIC_STICKER_SIZE_KB", "512"),
            max_video_sticker_size_kb=positive_int("MAX_VIDEO_STICKER_SIZE_KB", "256"),
            max_job_input_mb=positive_int("MAX_JOB_INPUT_MB", "500"),
            job_result_ttl_seconds=positive_int("JOB_RESULT_TTL_SECONDS", "3600"),
            pack_title_suffix=os.getenv("PACK_TITLE_SUFFIX", "by @mohonovproduction").strip(),
            log_channel_id=(int(os.getenv("LOG_CHANNEL_ID", "0").strip() or "0") or None),
            recipe_ttl_seconds=positive_int("RECIPE_TTL_SECONDS", "3600"),
        )
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        settings.download_dir.mkdir(parents=True, exist_ok=True)
        return settings
