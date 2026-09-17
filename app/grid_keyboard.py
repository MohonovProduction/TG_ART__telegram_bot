import re
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

CONFIRM = '✅ Подтвердить сетку'
CHANGE = '↩️ Выбрать другую сетку'


def grid_keyboard(columns=0, rows=0):
    # aiogram 3.20 forwards new Bot API fields through its extra-field support.
    keyboard = [
        [
            KeyboardButton(text=f'{x}×{y}', style='success')
            if x <= columns and y <= rows
            else KeyboardButton(text=f'{x}×{y}')
            for x in range(1, 13)
        ]
        for y in range(1, 13)
    ]
    if columns and rows:
        keyboard.append([KeyboardButton(text=CONFIRM), KeyboardButton(text=CHANGE)])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def parse_grid(text):
    match = re.fullmatch(r'\s*[□■]?\s*(\d{1,2})\s*[xх×]\s*(\d{1,2})\s*', text, re.I)
    if not match:
        raise ValueError('Нажмите клетку или введите размер, например 5×3.')
    columns, rows = map(int, match.groups())
    if not 1 <= columns <= 20 or not 1 <= rows <= 20 or columns * rows > 200:
        raise ValueError('Размер от 1×1 до 20×20, максимум 200 ячеек.')
    return columns, rows


async def confirmed_grid(message, state):
    text = (message.text or '').strip()
    if text == CHANGE:
        await state.update_data(pending_grid=None)
        await message.answer('Выберите правый нижний угол сетки.', reply_markup=grid_keyboard())
        return None
    if text == CONFIRM:
        pending = (await state.get_data()).get('pending_grid')
        if pending:
            await state.update_data(pending_grid=None)
            return tuple(pending)
        await message.answer('Сначала выберите размер сетки.', reply_markup=grid_keyboard())
        return None
    try:
        columns, rows = parse_grid(text)
    except ValueError as error:
        await message.answer(str(error))
        return None
    await state.update_data(pending_grid=[columns, rows])
    await message.answer(f'Нужна сетка {columns}×{rows}, {columns * rows} ячеек?', reply_markup=grid_keyboard(columns, rows))
    return None
