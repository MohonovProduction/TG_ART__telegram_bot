from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.exceptions import TelegramAPIError
from app.post_analysis import send_analysis

router = Router(name='post_analysis')
settings = None
reset_flow = None


class AnalysisFlow(StatesGroup):
    collecting = State()


async def start_analysis(message, state):
    await reset_flow(state)
    await state.set_state(AnalysisFlow.collecting)
    await state.update_data(analysis_messages=[])
    await message.answer('Перешлите пост или альбом, затем отправьте /done. Можно анализировать и сообщения без пересылки. /cancel — отмена.', reply_markup=ReplyKeyboardRemove())


@router.message(Command('analyze'))
async def analyze_command(message, state):
    await start_analysis(message, state)


@router.message(AnalysisFlow.collecting, Command('done'))
@router.message(AnalysisFlow.collecting, F.text == '🔍 Анализировать')
async def finish_analysis(message, state, bot):
    data = await state.get_data()
    raw_messages = data.get('analysis_messages', [])
    if not raw_messages:
        await message.answer('Сначала перешлите сообщение для анализа.')
        return
    messages = [Message.model_validate(item) for item in raw_messages]
    try:
        await send_analysis(messages, bot, message.chat.id, settings.temp_dir)
    except (TelegramAPIError, OSError) as error:
        await message.answer('Не удалось завершить анализ. Попробуйте /done ещё раз. Ошибка: ' + type(error).__name__)
        return
    await state.clear()


@router.message(AnalysisFlow.collecting)
async def collect(message, state):
    data = await state.get_data()
    messages = data.get('analysis_messages', [])
    if len(messages) >= 20:
        await message.answer('Достигнут лимит: 20 сообщений за анализ. Отправьте /done.')
        return
    if any(item['message_id'] == message.message_id for item in messages):
        return
    messages.append(message.model_dump(mode='json', exclude_none=True))
    await state.update_data(analysis_messages=messages)
    # Avoid generating a reply for every item of an album.
    if not message.media_group_id or not any(item.get('media_group_id') == message.media_group_id for item in messages[:-1]):
        await message.answer('Принято. Когда пост или альбом переслан полностью, нажмите «Анализировать».', reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='🔍 Анализировать')]], resize_keyboard=True))
