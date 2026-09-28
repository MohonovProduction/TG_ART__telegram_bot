"""Reply-keyboard settings used by both menu-first and message-first video notes."""
from pathlib import Path
from aiogram import Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from app.tgs_renderer import normalize_hex_color

router = Router(name='video_note_settings')
render = None
BACK = '↩️ Назад'
CANCEL = '❌ Отмена'
MODES = {'✂️ Cover — обрезать': 'cover', '↔️ Fit — вписать': 'fit', '🔲 Fill — растянуть': 'fill'}
POSITIONS = {'⬆️ Верх': 'top', '⬅️ Лево': 'left', '🎯 Центр': 'center', '➡️ Право': 'right', '⬇️ Низ': 'bottom'}
BACKGROUNDS = {'⚫ Чёрный': '#000000', '⚪ Белый': '#FFFFFF', '🌫 Размытое видео': 'blur'}
HEX = '🎨 Ввести HEX'


def keyboard(rows):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=text) for text in row] for row in rows], resize_keyboard=True)


MODE_KEYBOARD = keyboard([[key] for key in MODES] + [[CANCEL]])
POSITION_KEYBOARD = keyboard([['⬆️ Верх'], ['⬅️ Лево', '🎯 Центр', '➡️ Право'], ['⬇️ Низ'], [BACK, CANCEL]])
BACKGROUND_KEYBOARD = keyboard([['⚫ Чёрный', '⚪ Белый'], ['🌫 Размытое видео'], [HEX], [BACK, CANCEL]])
HEX_KEYBOARD = keyboard([[BACK, CANCEL]])


class VideoNoteFlow(StatesGroup):
    mode = State()
    position = State()
    background = State()
    hex_color = State()
    rendering = State()


async def start_options(message, state, source, temporary):
    await state.update_data(video_note_source=str(source), video_note_temporary=temporary)
    await state.set_state(VideoNoteFlow.mode)
    await message.answer(
        'Как разместить видео в кружке?\n\n'
        '• <b>Cover</b> — обрезать края.\n'
        '• <b>Fit</b> — вписать с полями.\n'
        '• <b>Fill</b> — растянуть.\n\n'
        'Углы видео скроются под круглой маской Telegram.',
        reply_markup=MODE_KEYBOARD,
    )


async def execute(message, state, **options):
    data = await state.get_data()
    await state.set_state(VideoNoteFlow.rendering)
    await render(message, state, Path(data['video_note_source']), data['video_note_temporary'], **options)


@router.message(VideoNoteFlow.mode)
async def choose_mode(message, state):
    mode = MODES.get(message.text)
    if mode == 'fill':
        await execute(message, state, mode='fill')
    elif mode == 'cover':
        await state.set_state(VideoNoteFlow.position)
        await message.answer('Выберите положение обрезки.', reply_markup=POSITION_KEYBOARD)
    elif mode == 'fit':
        await state.set_state(VideoNoteFlow.background)
        await message.answer('Выберите фон полей. Видео будет по центру.', reply_markup=BACKGROUND_KEYBOARD)
    else:
        await message.answer('Выберите режим кнопкой.', reply_markup=MODE_KEYBOARD)


@router.message(VideoNoteFlow.position)
async def choose_position(message, state):
    if message.text == BACK:
        await state.set_state(VideoNoteFlow.mode)
        await message.answer('Выберите режим.', reply_markup=MODE_KEYBOARD)
    elif message.text in POSITIONS:
        await execute(message, state, mode='cover', position=POSITIONS[message.text])
    else:
        await message.answer('Выберите положение кнопкой.', reply_markup=POSITION_KEYBOARD)


@router.message(VideoNoteFlow.background)
async def choose_background(message, state):
    if message.text == BACK:
        await state.set_state(VideoNoteFlow.mode)
        await message.answer('Выберите режим.', reply_markup=MODE_KEYBOARD)
    elif message.text == HEX:
        await state.set_state(VideoNoteFlow.hex_color)
        await message.answer('Введите HEX цвета, например #242424.', reply_markup=HEX_KEYBOARD)
    elif message.text in BACKGROUNDS:
        await execute(message, state, mode='fit', background=BACKGROUNDS[message.text])
    else:
        await message.answer('Выберите фон кнопкой.', reply_markup=BACKGROUND_KEYBOARD)


@router.message(VideoNoteFlow.hex_color)
async def receive_hex(message, state):
    if message.text == BACK:
        await state.set_state(VideoNoteFlow.background)
        await message.answer('Выберите фон полей.', reply_markup=BACKGROUND_KEYBOARD)
        return
    try:
        color = normalize_hex_color(message.text or '')
    except ValueError:
        await message.answer('Нужен HEX в формате #RRGGBB, например #242424.', reply_markup=HEX_KEYBOARD)
        return
    await execute(message, state, mode='fit', background=color)
