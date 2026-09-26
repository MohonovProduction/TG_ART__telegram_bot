"""Best-effort audit messages for non-owner activity."""
from __future__ import annotations

import html
from aiogram import Bot
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from aiogram.types import MessageEntity


class ActivityLogger:
    def __init__(self, channel_id: int | None, owner_id: int):
        self.channel_id = channel_id
        self.owner_id = owner_id

    async def event(self, bot: Bot, user, action: str, result: str, pack_url: str | None = None) -> None:
        if not self.channel_id or not user or user.id == self.owner_id:
            return
        label = ' '.join(part for part in [user.first_name, user.last_name] if part) or 'Без имени'
        username = f' @{user.username}' if user.username else ''
        link = f'\nПак: {html.escape(pack_url)}' if pack_url else ''
        try:
            await bot.send_message(self.channel_id, f'<b>{html.escape(action)}</b> — {html.escape(result)}\n{html.escape(label)}{username} · <code>{user.id}</code>{link}')
        except Exception:
            # Logging must never break a user's rendering task.
            return

    async def tg_art(self, bot: Bot, user, pack_url: str, text: str, entities: list[MessageEntity]) -> None:
        await self.event(bot, user, 'TG Art', 'пак создан', pack_url)
        if not self.channel_id or not user or user.id == self.owner_id:
            return
        try:
            await bot.send_message(self.channel_id, text, entities=entities, parse_mode=None)
        except Exception:
            return


class AuditMiddleware(BaseMiddleware):
    """Logs accepted user interactions after access control has allowed them."""
    def __init__(self, logger: ActivityLogger):
        self.logger = logger

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        user = getattr(event, "from_user", None)
        if isinstance(event, Message):
            if event.text:
                action = event.text.split()[0][:80]
            elif event.document:
                action = f"файл: {event.document.file_name or 'без имени'}"
            elif event.photo:
                action = "изображение"
            elif event.video:
                action = "видео"
            else:
                action = event.content_type.value
        elif isinstance(event, CallbackQuery):
            action = f"кнопка: {(event.data or '')[:80]}"
        else:
            return result
        await self.logger.event(data["bot"], user, action, "обработано")
        return result
