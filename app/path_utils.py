from __future__ import annotations

from pathlib import Path


def strip_wrapping_quotes(value: str) -> str:
    """Remove one matching quote pair commonly added by Finder's Copy as Pathname."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def parse_local_path(value: str) -> Path:
    return Path(strip_wrapping_quotes(value)).expanduser().resolve()
