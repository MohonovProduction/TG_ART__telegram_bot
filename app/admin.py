import html
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from app import access

reset_flow = None
start_flow = None

router = Router(name='administration')


class AdminFlow(StatesGroup):
    target = State()


ACTIONS = {'➕ Добавить пользователя': 'user', '⭐ Назначить администратора': 'admin', '🚫 Отозвать доступ': None}


@router.message(Command('users'))
async def users(message, state):
    role = access.store.role(message.from_user.id)
    if role not in ('owner', 'admin'):
        await message.answer('У вас нет прав управления пользователями.')
        return
    entries = access.store.users()
    text = 'Пользователи:\n' + '\n'.join(f'{uid}: {r}' for uid, r in entries)
    for offset in range(0, len(text), 3500):
        await message.answer(html.escape(text[offset:offset + 3500]))
    buttons = [KeyboardButton(text=x) for x in ACTIONS if role == 'owner' or ACTIONS[x] != 'admin']
    await message.answer('Выберите действие.', reply_markup=ReplyKeyboardMarkup(keyboard=[[b] for b in buttons], resize_keyboard=True))


@router.message(F.text.in_(ACTIONS))
async def action(message, state):
    role = access.store.role(message.from_user.id)
    choice = ACTIONS[message.text]
    if role not in ('owner', 'admin') or (choice == 'admin' and role != 'owner'):
        await message.answer('Недостаточно прав.')
        return
    # Release files from any interrupted rendering conversation.
    await reset_flow(state)
    await state.update_data(admin_action=choice)
    await state.set_state(AdminFlow.target)
    await message.answer('Отправьте Telegram ID или контакт с Telegram ID. /cancel — отмена.', reply_markup=ReplyKeyboardRemove())


@router.message(Command('cancel'))
async def cancel(message, state):
    await reset_flow(state)
    await message.answer('Отменено. /start — функции бота.', reply_markup=ReplyKeyboardRemove())


@router.message(Command('start'))
async def restart(message, state):
    await start_flow(message, state)


@router.message(AdminFlow.target)
async def target(message, state):
    raw = message.contact.user_id if message.contact else (message.text or '').strip()
    try:
        uid = int(raw)
        data = await state.get_data()
        access.store.change(message.from_user.id, uid, data['admin_action'])
    except (TypeError, ValueError) as error:
        await message.answer(html.escape(str(error)) if isinstance(error, ValueError) and not str(error).startswith('invalid literal') else 'Нужен числовой Telegram ID. В контакте должен быть указан Telegram ID.')
        return
    await state.clear()
    await message.answer(f'Доступ для {uid} обновлён. /users — управление, /start — функции бота.')
