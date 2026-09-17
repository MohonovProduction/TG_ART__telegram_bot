from aiogram.exceptions import TelegramBadRequest
from app.sticker_pack import make_sticker_set_name


async def available_pack_name(bot, value, username):
    original = make_sticker_set_name(value, username)
    suffix = '_by_' + username.lstrip('@').lower()
    base = original[:-len(suffix)]
    for index in range(100):
        ending = '' if index == 0 else f'_{index + 1}'
        capacity = 64 - len(suffix) - len(ending)
        if capacity < 1:
            raise ValueError('Не хватает места для другого имени. Укажите более короткий username бота.')
        candidate = base[:capacity].rstrip('_') + ending + suffix
        try:
            await bot.get_sticker_set(candidate)
        except TelegramBadRequest as error:
            if 'STICKERSET_INVALID' in str(error).upper() or 'STICKER SET NOT FOUND' in str(error).upper():
                return candidate
            raise
    raise ValueError('Не удалось подобрать свободное имя. Введите другое название.')
