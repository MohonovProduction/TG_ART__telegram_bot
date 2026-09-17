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
    pack_title_suffix: str = "by @mohonovproduction"

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

        settings = cls(
            access_db=project_path("ACCESS_DB", "./data/access.sqlite3").resolve(),
            allow_local_paths=os.getenv("ALLOW_LOCAL_PATHS", "true").lower() == "true",
            max_concurrent_jobs=max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "2"))),
            bot_token=token,
            allowed_user_id=int(user_id),
            output_dir=project_path("OUTPUT_DIR", "./output").resolve(),
            temp_dir=project_path("TEMP_DIR", "./temp").resolve(),
            download_dir=project_path("DOWNLOAD_DIR", "./downloads").resolve(),
            default_fps=min(30, max(1, int(os.getenv("DEFAULT_FPS", "30")))),
            default_duration=min(3.0, max(0.1, float(os.getenv("DEFAULT_DURATION", "3")))),
            max_emoji_size_kb=int(os.getenv("MAX_EMOJI_SIZE_KB", "256")),
            max_static_sticker_size_kb=int(
                os.getenv("MAX_STATIC_STICKER_SIZE_KB", "512")
            ),
            max_video_sticker_size_kb=int(
                os.getenv("MAX_VIDEO_STICKER_SIZE_KB", "256")
            ),
            pack_title_suffix=os.getenv("PACK_TITLE_SUFFIX", "by @mohonovproduction").strip(),
        )
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        settings.download_dir.mkdir(parents=True, exist_ok=True)
        return settings
