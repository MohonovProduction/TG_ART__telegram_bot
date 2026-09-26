"""Bounded in-memory coordination for expensive media jobs."""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class JobAlreadyRunning(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


class HeavyJobQueue:
    """Run a limited number of FFmpeg/TGS jobs without blocking light requests."""

    def __init__(self, concurrency: int) -> None:
        self.concurrency = concurrency
        self._slots = asyncio.Semaphore(concurrency)
        self._guard = asyncio.Lock()
        self._users: set[int] = set()
        self._waiting: list[int] = []
        self._cancellations: dict[int, asyncio.Event] = {}
        self._active_tasks: dict[int, asyncio.Task] = {}

    async def run(self, user_id: int, queued: Callable[[int], Awaitable[None]], work: Callable[[], Awaitable[T]]) -> T:
        async with self._guard:
            if user_id in self._users:
                raise JobAlreadyRunning("У вас уже выполняется или ожидает очередь тяжёлая задача.")
            position = len(self._users) + 1
            self._users.add(user_id)
            self._waiting.append(user_id)
            cancellation = asyncio.Event()
            self._cancellations[user_id] = cancellation
        try:
            if position > self.concurrency:
                await queued(position)
            acquire = asyncio.create_task(self._slots.acquire())
            cancelled = asyncio.create_task(cancellation.wait())
            done, pending = await asyncio.wait({acquire, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                acquire.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await acquire
                raise JobCancelled("Задача удалена из очереди.")
            cancelled.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancelled
            await acquire
            try:
                async with self._guard:
                    self._waiting.remove(user_id)
                    self._active_tasks[user_id] = asyncio.current_task()
                return await work()
            finally:
                self._slots.release()
        finally:
            async with self._guard:
                self._users.discard(user_id)
                self._cancellations.pop(user_id, None)
                self._active_tasks.pop(user_id, None)
                if user_id in self._waiting:
                    self._waiting.remove(user_id)

    def cancel(self, user_id: int) -> bool:
        """Cancel a queued job or its handler while it is actively rendering."""
        cancellation = self._cancellations.get(user_id)
        if cancellation and user_id in self._waiting:
            cancellation.set()
            return True
        task = self._active_tasks.get(user_id)
        if task and not task.done():
            task.cancel()
            return True
        return False


@dataclass(frozen=True)
class Preview:
    data: dict
    expires_at: float


class PreviewCache:
    """A size-bounded TTL cache for callback payloads; never persists user data."""

    def __init__(self, ttl_seconds: int = 1800, max_items: int = 200) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._items: OrderedDict[str, Preview] = OrderedDict()

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [key for key, item in self._items.items() if item.expires_at <= now]:
            self._items.pop(token, None)

    def put(self, token: str, data: dict) -> None:
        self._purge()
        self._items[token] = Preview(data=data, expires_at=time.monotonic() + self.ttl_seconds)
        self._items.move_to_end(token)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def get(self, token: str) -> dict | None:
        self._purge()
        item = self._items.get(token)
        return item.data if item else None

    def discard(self, token: str) -> None:
        self._items.pop(token, None)


def cleanup_expired_job_directories(root: Path, max_age_seconds: int = 3600) -> int:
    """Remove only completed renderer job directories, never user-upload source files."""
    if not root.exists():
        return 0
    threshold = time.time() - max_age_seconds
    prefixes = ("stickers_", "video_note_", "archive_")
    removed = 0
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith(prefixes) and path.stat().st_mtime < threshold:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed
