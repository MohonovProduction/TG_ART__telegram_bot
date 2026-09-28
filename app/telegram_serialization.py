"""JSON-safe serialization for incoming aiogram models.

Incoming objects may contain ``aiogram.client.default.Default`` in nested
optional fields. It is a client-side sentinel rather than Telegram data and
Pydantic cannot serialize it in JSON mode.
"""
from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Any

from aiogram.client.default import Default


_OMIT = object()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Default):
        return _OMIT
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python", exclude_none=True)
    if isinstance(value, dict):
        return {
            str(key): converted
            for key, item in value.items()
            if (converted := _json_safe(item)) is not _OMIT
        }
    if isinstance(value, (list, tuple)):
        return [converted for item in value if (converted := _json_safe(item)) is not _OMIT]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def telegram_model_dump(model: Any) -> dict[str, Any]:
    """Return an incoming Telegram model without aiogram-only defaults."""
    dumped = model.model_dump(mode="python", exclude_none=True)
    result = _json_safe(dumped)
    if not isinstance(result, dict):
        raise TypeError("Expected a Telegram model to serialize to a dictionary")
    return result
