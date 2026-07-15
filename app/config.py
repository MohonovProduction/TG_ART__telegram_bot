from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_user_id: int
    output_dir: Path
    temp_dir: Path
    default_fps: int = 30
    default_duration: float = 3.0
    max_emoji_size_kb: int = 256
    pack_title_suffix: str = "by @mohonovproduction"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        user_id = os.getenv("ALLOWED_USER_ID", "").strip()
        if not token or token == "put_your_bot_token_here":
            raise RuntimeError("Укажите BOT_TOKEN в файле .env")
        if not user_id.isdigit():
            raise RuntimeError("Укажите числовой ALLOWED_USER_ID в файле .env")

        settings = cls(
            bot_token=token,
            allowed_user_id=int(user_id),
            output_dir=Path(os.getenv("OUTPUT_DIR", "./output")).expanduser().resolve(),
            temp_dir=Path(os.getenv("TEMP_DIR", "./temp")).expanduser().resolve(),
            default_fps=min(30, max(1, int(os.getenv("DEFAULT_FPS", "30")))),
            default_duration=min(3.0, max(0.1, float(os.getenv("DEFAULT_DURATION", "3")))),
            max_emoji_size_kb=int(os.getenv("MAX_EMOJI_SIZE_KB", "256")),
            pack_title_suffix=os.getenv("PACK_TITLE_SUFFIX", "by @mohonovproduction").strip(),
        )
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        return settings
