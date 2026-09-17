"""Persistent allowlist and role hierarchy shared by all bot flows."""
import asyncio
import sqlite3
from pathlib import Path
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


class AccessStore:
    def __init__(self, path: Path, owner_id: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.owner_id = owner_id
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, role TEXT NOT NULL)')
        self.db.commit()

    def role(self, user_id):
        if user_id == self.owner_id:
            return 'owner'
        row = self.db.execute('SELECT role FROM users WHERE id=?', (user_id,)).fetchone()
        return row[0] if row else None

    def change(self, actor, target, role):
        actor_role = self.role(actor)
        if actor_role not in ('owner', 'admin'):
            raise ValueError('У вас нет прав управления пользователями.')
        if target <= 0 or target == self.owner_id:
            raise ValueError('Владельца нельзя изменить; ID должен быть положительным.')
        if role not in ('user', 'admin', None):
            raise ValueError('Неизвестная роль.')
        if actor_role == 'admin' and (role == 'admin' or self.role(target) == 'admin'):
            raise ValueError('Администратор может управлять только обычными пользователями.')
        if role is None:
            self.db.execute('DELETE FROM users WHERE id=?', (target,))
        else:
            self.db.execute('INSERT INTO users VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET role=excluded.role', (target, role))
        self.db.commit()

    def users(self):
        return [(self.owner_id, 'owner')] + self.db.execute('SELECT id, role FROM users WHERE id != ? ORDER BY id', (self.owner_id,)).fetchall()


store = None


def allowed(user_id):
    return store is not None and store.role(user_id) is not None


class AccessMiddleware(BaseMiddleware):
    def __init__(self, concurrency=2):
        self.locks = {}
        self.slots = asyncio.Semaphore(concurrency)

    async def __call__(self, handler, event, data):
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)
        user = event.from_user
        if not user or not allowed(user.id):
            await event.answer('Доступ закрыт. Обратитесь к владельцу бота.')
            return
        chat = event.chat if isinstance(event, Message) else getattr(event.message, 'chat', None)
        if not chat or chat.type != 'private':
            await event.answer('Используйте бота в личных сообщениях.')
            return
        lock = self.locks.setdefault(user.id, asyncio.Lock())
        if lock.locked() and data.get("raw_state") != "AnalysisFlow:collecting":
            await event.answer('Предыдущий запрос ещё обрабатывается. Дождитесь завершения.')
            return
        async with lock:
            if data.get("state") is not None:
                data["raw_state"] = await data["state"].get_state()
            async with self.slots:
                if not allowed(user.id):
                    await event.answer("Доступ отозван.")
                    return
                return await handler(event, data)
