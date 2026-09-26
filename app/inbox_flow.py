"""Content-first actions and explicit replacement of unfinished conversations."""
import html
import secrets
from pathlib import Path

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import Message, ReplyKeyboardRemove
from app import access, video_note_flow
from app.media import detect_source_kind
from app.rich_message import rich_payload, walk_tree
from app.renderer import probe_video, RenderError
from app.post_analysis import send_analysis
from app.telegram_retry import retry_telegram

router = Router(name='content_first')
config = None
ART = '🎨 Создать TG Art'
STICKERS = '🖼 Создать стикерпак'
NOTE = '⭕ Сделать кружок'
ANALYZE = '🔍 Анализировать'
USER = '👤 Назначить пользователем'
ADMIN = '⭐ Назначить администратором'
REVOKE = '🚫 Отозвать доступ'
NEW = '🆕 Начать с новым файлом'
CONTINUE = '↩️ Продолжить текущую операцию'
DONE = '✅ Завершить добавление'
CANCEL = '❌ Отмена'
COLLECT_KEYBOARD = video_note_flow.keyboard([[DONE], [CANCEL]])
SWITCH_KEYBOARD = video_note_flow.keyboard([[NEW], [CONTINUE], [CANCEL]])


class InboxFlow(StatesGroup):
    choosing = State()
    switching = State()
    processing = State()
    sticker_kind = State()


def media_sources(message):
    """Metadata only; downloads happen only after an action is selected."""
    sources = []
    if message.photo:
        photo = max(message.photo, key=lambda item: item.width * item.height)
        sources.append({'kind': 'image', 'file_id': photo.file_id, 'suffix': '.jpg', 'file_size': photo.file_size or 0})
    for attr in ('video', 'animation', 'video_note', 'document'):
        attachment = getattr(message, attr, None)
        if attachment:
            filename = getattr(attachment, 'file_name', None) or 'source'
            suffix = Path(filename).suffix
            try:
                kind = 'video' if attr != 'document' else detect_source_kind(Path(filename), attachment.mime_type)
            except ValueError:
                continue
            sources.append({'kind': kind, 'file_id': attachment.file_id, 'suffix': suffix or ('.mp4' if kind == 'video' else '.png'), 'file_size': getattr(attachment, 'file_size', None) or 0})
    rich = rich_payload(message)
    if rich:
        for _, block in walk_tree(rich):
            kind = block.get('type')
            if kind in ('photo', 'video', 'animation', 'document') and kind in block:
                nested = Message.model_validate({'message_id': message.message_id, 'date': message.date, 'chat': message.chat.model_dump(), kind: block[kind]})
                sources.extend(media_sources(nested))
    return sources


def available_actions(messages, actor):
    if len(messages) == 1 and messages[0].contact:
        target = messages[0].contact.user_id
        role = access.store.role(actor)
        target_role = access.store.role(target) if target else None
        if target and target_role != 'owner' and role in ('owner', 'admin') and (role == 'owner' or target_role != 'admin'):
            return [USER] + ([ADMIN] if role == 'owner' else []) + [REVOKE]
        return [ANALYZE]
    media = [source for message in messages for source in media_sources(message)]
    album = any(message.media_group_id for message in messages)
    actions = []
    if len(media) == 1 and not album:
        actions.append(ART)
    if media:
        actions.append(STICKERS)
    if len(media) == 1 and media[0]['kind'] == 'video' and not album:
        actions.append(NOTE)
    actions.append(ANALYZE)
    return actions


def action_keyboard(messages, actor):
    actions = available_actions(messages, actor)
    rows = []
    if ART in actions and STICKERS in actions:
        rows.append([ART, STICKERS])
        actions = [x for x in actions if x not in (ART, STICKERS)]
    rows.extend([[action] for action in actions])
    return video_note_flow.keyboard(rows + [[CANCEL]])


def received_content(message):
    return bool(message.photo or message.video or message.animation or message.video_note or message.document or message.contact or message.forward_origin or rich_payload(message))


def stored_messages(data, key='inbox_messages'):
    return [Message.model_validate(raw) for raw in data.get(key, [])]


async def offer(message, state):
    await state.set_state(InboxFlow.choosing)
    await state.update_data(inbox_messages=[message.model_dump(mode='json', exclude_none=True)])
    await message.answer('Сообщение получено. Что сделать с ним? Если это альбом, дождитесь получения всех его частей.', reply_markup=action_keyboard([message], message.from_user.id))


async def restore(message, state):
    data = await state.get_data()
    original = data.get('inbox_resume_data', {})
    previous = data.get('inbox_resume_state')
    await state.set_data(original)
    await state.set_state(previous)
    await message.answer('Продолжаем предыдущую операцию. ' + config.phase_hint(previous), reply_markup=config.resume_keyboard(previous, original, message.from_user.id))


async def replace_pending(message, state):
    data = await state.get_data()
    incoming = stored_messages(data)
    if not incoming:
        await message.answer('Полученное сообщение уже недоступно. Отправьте его заново.')
        return
    await state.set_data(data.get('inbox_resume_data', {}))
    await state.set_state(data.get('inbox_resume_state'))
    await config.reset(state)
    await state.set_state(InboxFlow.choosing)
    await state.update_data(inbox_messages=[m.model_dump(mode='json', exclude_none=True) for m in incoming])
    await message.answer('Выберите действие с новым содержимым.', reply_markup=action_keyboard(incoming, message.from_user.id))


class ContentFirstMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not isinstance(event, Message) or not received_content(event):
            return await handler(event, data)
        state = data['state']
        current = await state.get_state()
        old_data = await state.get_data()
        # Expected inputs remain part of the current conversation.
        if current == 'AnalysisFlow:collecting' or (current == 'AdminFlow:target' and event.contact):
            return await handler(event, data)
        if current == 'RenderFlow:waiting_for_existing_tg_art_source' and event.text:
            return await handler(event, data)
        if current == 'RenderFlow:waiting_for_source' and media_sources(event) and not rich_payload(event):
            return await handler(event, data)
        if current == 'RenderFlow:waiting_for_video_note_source' and len(media_sources(event)) == 1 and media_sources(event)[0]['kind'] == 'video' and not rich_payload(event):
            return await handler(event, data)
        if current == 'RenderFlow:collecting_stickers' and media_sources(event) and not rich_payload(event):
            expected = 'image' if old_data['sticker_format'] == 'static' else 'video'
            if all(source['kind'] == expected for source in media_sources(event)):
                await append_stickers(event, state, data['bot'])
                return
        if current == 'DownloadFlow:collecting' and (event.sticker or event.text):
            return await handler(event, data)
        if current in (InboxFlow.choosing.state, InboxFlow.switching.state, InboxFlow.sticker_kind.state):
            existing = stored_messages(old_data)
            if existing and event.media_group_id and existing[0].media_group_id == event.media_group_id:
                if len(existing) >= 20:
                    await event.answer('Лимит: 20 сообщений. Выберите действие с уже полученными.')
                    return
                previous_actions = available_actions(existing, event.from_user.id)
                if not any(m.message_id == event.message_id for m in existing):
                    existing.append(event)
                await state.update_data(inbox_messages=[m.model_dump(mode='json', exclude_none=True) for m in existing])
                if current == InboxFlow.choosing.state and available_actions(existing, event.from_user.id) != previous_actions:
                    await event.answer(f'Получено элементов альбома: {len(existing)}. Выберите действие после получения всех частей.', reply_markup=action_keyboard(existing, event.from_user.id))
                return
            if current == InboxFlow.switching.state:
                await state.update_data(inbox_messages=[event.model_dump(mode='json', exclude_none=True)])
                await event.answer('Получено новое содержимое. Выберите, продолжить ли прежнюю операцию.', reply_markup=SWITCH_KEYBOARD)
                return
        if current is None or current == 'RenderFlow:waiting_for_mode':
            await config.reset(state)
            await offer(event, state)
        else:
            await state.set_data({'inbox_resume_state': current, 'inbox_resume_data': old_data, 'inbox_messages': [event.model_dump(mode='json', exclude_none=True)]})
            await state.set_state(InboxFlow.switching)
            await event.answer('Сейчас идёт другая операция. ' + config.phase_hint(current) + '\nИспользовать полученное содержимое для новой операции?', reply_markup=SWITCH_KEYBOARD)
        return


async def download_source(source, message, bot):
    suffix = source['suffix']
    # Use only a safe extension rather than a remote filename.
    if not suffix.startswith('.') or len(suffix) > 12 or not suffix[1:].isalnum():
        suffix = '.mp4' if source['kind'] == 'video' else '.png'
    destination = config.settings.temp_dir / f'{message.from_user.id}_{secrets.token_hex(12)}{suffix}'
    try:
        await retry_telegram(lambda: bot.download(source['file_id'], destination=destination))
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


async def append_stickers(message, state, bot, sources=None):
    data = await state.get_data()
    sources = media_sources(message) if sources is None else sources
    expected = 'image' if data['sticker_format'] == 'static' else 'video'
    if any(source['kind'] != expected for source in sources):
        await message.answer('Тип файла не подходит к этому паку. Отправьте изображение для статичного пака или видео для видео-пака.', reply_markup=COLLECT_KEYBOARD)
        return
    paths = list(data.get('sticker_sources', []))
    limit = 120 if expected == 'image' else 50
    if len(paths) + len(sources) > limit:
        await message.answer(f'В паке может быть не больше {limit} стикеров.', reply_markup=COLLECT_KEYBOARD)
        return
    existing_size = sum(Path(path).stat().st_size for path in paths if Path(path).is_file())
    incoming_size = sum(int(source.get('file_size') or 0) for source in sources)
    if existing_size + incoming_size > config.settings.max_job_input_mb * 1024 * 1024:
        await message.answer(
            f'Лимит исходников для одной задачи — {config.settings.max_job_input_mb} MB. '
            'Завершите текущую пачку или отправьте меньший набор.', reply_markup=COLLECT_KEYBOARD
        )
        return
    temporary = list(data.get('temporary_sticker_sources', []))
    # Persist each file immediately so cancel can clean up a partial batch.
    for source in sources:
        try:
            path = await download_source(source, message, bot)
        except (TelegramAPIError, OSError):
            await message.answer('Не удалось скачать файл. Отправьте его повторно; уже добавленные файлы сохранены.', reply_markup=COLLECT_KEYBOARD)
            return
        paths.append(str(path))
        temporary.append(str(path))
        await state.update_data(sticker_sources=paths, temporary_sticker_sources=temporary)
    await message.answer(f'Добавлено файлов: {len(paths)}. Отправьте ещё файлы или завершите добавление.', reply_markup=COLLECT_KEYBOARD)


@router.message(InboxFlow.switching, F.text != CANCEL, ~F.text.startswith("/"))
async def choose_replacement(message, state):
    if message.text == CONTINUE:
        await restore(message, state)
    elif message.text == NEW:
        await replace_pending(message, state)
    else:
        await message.answer('Выберите новую или предыдущую операцию.', reply_markup=SWITCH_KEYBOARD)


@router.message(InboxFlow.choosing, F.text != CANCEL, ~F.text.startswith("/"))
async def choose_action(message, state, bot):
    messages = stored_messages(await state.get_data())
    action = message.text
    if action not in available_actions(messages, message.from_user.id):
        await message.answer('Выберите доступное действие с полученным содержимым.', reply_markup=action_keyboard(messages, message.from_user.id))
        return
    try:
        if action in (USER, ADMIN, REVOKE):
            target = messages[0].contact.user_id
            access.store.change(message.from_user.id, target, {USER: 'user', ADMIN: 'admin', REVOKE: None}[action])
            await state.clear()
            await message.answer(f'Доступ для {target} обновлён. /users — управление пользователями.', reply_markup=ReplyKeyboardRemove())
        elif action == ANALYZE:
            await state.set_state(InboxFlow.processing)
            await message.answer('Анализирую полученное содержимое…', reply_markup=ReplyKeyboardRemove())
            await send_analysis(messages, bot, message.chat.id, config.settings.temp_dir)
            await state.clear()
        else:
            sources = [source for item in messages for source in media_sources(item)]
            if action == STICKERS:
                if len({source['kind'] for source in sources}) != 1:
                    await state.set_state(InboxFlow.sticker_kind)
                    await message.answer('В альбоме есть изображения и видео. Выберите тип пака: будут добавлены все подходящие файлы.', reply_markup=video_note_flow.keyboard([['Статичные', 'Видео'], [CANCEL]]))
                    return
                await config.reset(state)
                await config.start_stickers(message, state, 'static' if sources[0]['kind'] == 'image' else 'video')
                await append_stickers(message, state, bot, sources)
            else:
                await state.set_state(InboxFlow.processing)
                path = await download_source(sources[0], message, bot)
                if action == ART and sources[0]['kind'] == 'video':
                    try:
                        info = await probe_video(path)
                        if not info.has_alpha:
                            raise RenderError('Для TG Art нужно видео с alpha-каналом. Выберите стикерпак или кружок.')
                    except BaseException:
                        path.unlink(missing_ok=True)
                        raise
                await config.reset(state)
                if action == ART:
                    await config.set_source(message, state, path, True, sources[0]['kind'])
                else:
                    await video_note_flow.start_options(message, state, path, True)
    except (TelegramAPIError, RenderError, ValueError, OSError) as error:
        if await state.get_state() == InboxFlow.processing.state:
            await state.set_state(InboxFlow.choosing)
        await message.answer('Не удалось выполнить действие: ' + html.escape(str(error)[:1000]), reply_markup=action_keyboard(messages, message.from_user.id) if await state.get_state() == InboxFlow.choosing.state else COLLECT_KEYBOARD)


@router.message(StateFilter('RenderFlow:collecting_stickers'), F.text == DONE)
async def finish_stickers(message, state):
    await config.finish_stickers(message, state)


@router.message(StateFilter('RenderFlow:waiting_for_source'), F.video | F.animation)
async def accept_art_video(message, state, bot):
    path = None
    try:
        source = media_sources(message)[0]
        path = await download_source(source, message, bot)
        if not (await probe_video(path)).has_alpha:
            raise RenderError('Для TG Art нужно видео с alpha-каналом. Отправьте другой файл или /cancel.')
        await config.set_source(message, state, path, True, 'video')
    except (TelegramAPIError, OSError, RenderError) as error:
        if path and (await state.get_data()).get('source') != str(path):
            path.unlink(missing_ok=True)
        await message.answer(html.escape(str(error)[:1000]))


@router.message(InboxFlow.sticker_kind, F.text != CANCEL, ~F.text.startswith('/'))
async def choose_sticker_kind(message, state, bot):
    if message.text not in ('Статичные', 'Видео'):
        await message.answer('Выберите тип пака.', reply_markup=video_note_flow.keyboard([['Статичные', 'Видео'], [CANCEL]]))
        return
    expected = 'image' if message.text == 'Статичные' else 'video'
    messages = stored_messages(await state.get_data())
    sources = [source for item in messages for source in media_sources(item) if source['kind'] == expected]
    await config.reset(state)
    await config.start_stickers(message, state, 'static' if expected == 'image' else 'video')
    await append_stickers(message, state, bot, sources)
