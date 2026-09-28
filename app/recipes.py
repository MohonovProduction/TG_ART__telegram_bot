"""Short-lived recipes for immediately creating a corrected pack version."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Recipe:
    token: str
    owner_id: int
    pack_name: str
    pack_url: str
    kind: str
    title: str
    emoji: str
    columns: int | None = None
    rows: int | None = None
    sticker_format: str | None = None
    sticker_type: str = "regular"
    emojis: tuple[str, ...] = ()


class RecipeCache:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[float, Recipe]] = {}

    def put(self, recipe: Recipe) -> None:
        self._items[recipe.token] = (time.monotonic() + self.ttl_seconds, recipe)

    def get(self, token: str) -> Recipe | None:
        item = self._items.get(token)
        if not item or item[0] < time.monotonic():
            self._items.pop(token, None)
            return None
        return item[1]

    def discard(self, token: str) -> None:
        self._items.pop(token, None)
