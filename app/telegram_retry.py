"""Small, explicit retry policy for idempotent Telegram API operations."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError

T = TypeVar("T")
RetryNotice = Callable[[int, float], Awaitable[None]]


async def retry_telegram(
    operation: Callable[[], Awaitable[T]], *, attempts: int = 4, notice: RetryNotice | None = None
) -> T:
    """Retry only transient transport/server/rate-limit failures.

    Callers should use this for idempotent reads/uploads, not message delivery where
    an unknown response could create a duplicate user-visible message.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except TelegramRetryAfter as error:
            delay = float(error.retry_after)
        except (TelegramNetworkError, TelegramServerError):
            delay = float(2 ** (attempt - 1))
        if attempt == attempts:
            raise
        if notice:
            await notice(attempt, delay)
        await asyncio.sleep(delay)
    raise RuntimeError("unreachable")
