"""Post analysis and UTF-16-safe removal of custom emoji entities."""
import html
import json
import math
import tempfile
from pathlib import Path

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, MessageEntity, InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument, InputPollOption, BufferedInputFile
from app.rich_message import rich_payload, walk_tree, flatten_rich_message, replace_rich_emoji, input_rich_message, rich_structure, SendRichMessage, MEDIA_TYPES, block_text
from app.renderer import probe_video, RenderError


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def utf16_slice(text, offset, length):
    return text.encode('utf-16-le')[offset * 2:(offset + length) * 2].decode('utf-16-le')


def replace_custom_emoji(text, entities, stickers):
    entities = entities or []
    replacements = []
    for entity in sorted(entities, key=lambda e: e.offset):
        if entity.type != 'custom_emoji':
            continue
        original = utf16_slice(text, entity.offset, entity.length)
        sticker = stickers.get(entity.custom_emoji_id)
        replacement = (getattr(sticker, 'emoji', None) or original)
        replacements.append({'offset': entity.offset, 'length': entity.length, 'custom_emoji_id': entity.custom_emoji_id, 'original': original, 'replacement': replacement})
    raw = text.encode('utf-16-le')
    for entry in reversed(replacements):
        start, end = entry['offset'] * 2, (entry['offset'] + entry['length']) * 2
        raw = raw[:start] + entry['replacement'].encode('utf-16-le') + raw[end:]

    def boundary(position):
        delta = 0
        for entry in replacements:
            start, end = entry['offset'], entry['offset'] + entry['length']
            new_length = utf16_length(entry['replacement'])
            if position >= end:
                delta += new_length - entry['length']
            elif position > start:
                return start + delta + min(position - start, new_length)
            else:
                break
        return position + delta

    preserved = []
    for entity in entities:
        if entity.type == 'custom_emoji':
            continue
        start, end = boundary(entity.offset), boundary(entity.offset + entity.length)
        if end > start:
            preserved.append(entity.model_copy(update={'offset': start, 'length': end - start}))
    return raw.decode('utf-16-le'), preserved, replacements


def grid_structure(text, entities):
    counts = {}
    for entity in entities or []:
        if entity.type == 'custom_emoji':
            line = utf16_slice(text, 0, entity.offset).count('\n') + 1
            counts[line] = counts.get(line, 0) + 1
    lines = sorted(counts)
    rectangular = len(lines) > 1 and len(set(counts.values())) == 1 and lines == list(range(lines[0], lines[-1] + 1))
    return {'custom_emoji_per_line': counts, 'assumed_grid': {'columns': counts[lines[0]], 'rows': len(lines)} if rectangular else None}


def message_text(message):
    rich = rich_payload(message)
    if rich is not None:
        return flatten_rich_message(rich)
    return (message.text, message.entities or []) if message.text is not None else (message.caption or '', message.caption_entities or [])


async def media_metadata(message, bot, temp_dir):
    result = []
    if message.photo:
        photo = max(message.photo, key=lambda item: item.width * item.height)
        result.append({'type': 'photo', **photo.model_dump(mode='json', exclude_none=True), 'available_sizes': [x.model_dump(mode='json', exclude_none=True) for x in message.photo]})
    if message.game and message.game.photo:
        photo = max(message.game.photo, key=lambda item: item.width * item.height)
        result.append({'type': 'game_photo', **photo.model_dump(mode='json', exclude_none=True)})
    if message.paid_media:
        for attachment in message.paid_media.paid_media:
            photos = getattr(attachment, 'photo', None)
            video = getattr(attachment, 'video', None)
            if photos:
                photo = max(photos, key=lambda item: item.width * item.height)
                result.append({'type': 'paid_photo', **photo.model_dump(mode='json', exclude_none=True)})
            elif video:
                result.append({'type': 'paid_video', **video.model_dump(mode='json', exclude_none=True)})
            else:
                result.append({'type': 'paid_preview', **attachment.model_dump(mode='json', exclude_none=True)})
    for kind in ('video', 'animation', 'document', 'audio', 'voice', 'video_note', 'sticker'):
        media = getattr(message, kind, None)
        if not media:
            continue
        entry = {'type': kind, **media.model_dump(mode='json', exclude_none=True)}
        if kind == 'video_note':
            entry.update(width=media.length, height=media.length)
        if kind == 'document' and ((media.mime_type or '').startswith(('image/', 'video/')) or Path(media.file_name or '').suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.tiff', '.bmp', '.heic', '.avif', '.mov', '.mp4', '.webm', '.gif')):
            if media.file_size and media.file_size > 20 * 1024 * 1024:
                entry['probe_limitation'] = 'Файл больше 20 MB: размеры из содержимого недоступны через стандартный Bot API.'
            else:
                try:
                    with tempfile.TemporaryDirectory(prefix='post_analysis_', dir=temp_dir) as directory:
                        source = Path(directory) / ('source' + Path(media.file_name or '').suffix)
                        await bot.download(media, destination=source)
                        info = await probe_video(source)
                        entry.update(width=info.width, height=info.height, codec=info.codec, pixel_format=info.pixel_format)
                        if info.duration:
                            entry.update(duration=info.duration, fps=info.fps)
                except (TelegramAPIError, RenderError, OSError) as error:
                    entry['probe_limitation'] = 'Не удалось прочитать размеры файла: ' + str(error)[:200]
        if entry.get('width') and entry.get('height'):
            divisor = math.gcd(entry['width'], entry['height'])
            entry['aspect_ratio'] = f"{entry['width'] // divisor}:{entry['height'] // divisor}"
        result.append(entry)
    rich = rich_payload(message)
    if rich is not None:
        for path, block in walk_tree(rich):
            kind = block.get('type')
            if kind in MEDIA_TYPES and kind in block:
                field = 'voice' if kind == 'voice_note' else kind
                nested = Message.model_validate({'message_id': message.message_id, 'date': message.date, 'chat': message.chat.model_dump(), field: block[kind]})
                entries = await media_metadata(nested, bot, temp_dir)
                for entry in entries:
                    entry['rich_block_path'] = path
                result.extend(entries)
    for entry in result:
        if entry.get('width') and entry.get('height') and 'aspect_ratio' not in entry:
            divisor = math.gcd(entry['width'], entry['height'])
            entry['aspect_ratio'] = f"{entry['width'] // divisor}:{entry['height'] // divisor}"
    return result


def additional_text_fields(message):
    fields = []
    if message.poll:
        fields.append(('poll.question', message.poll.question, message.poll.question_entities or []))
        for index, option in enumerate(message.poll.options):
            fields.append((f'poll.options.{index}', option.text, option.text_entities or []))
        if message.poll.explanation:
            fields.append(('poll.explanation', message.poll.explanation, message.poll.explanation_entities or []))
    if message.game and message.game.text:
        fields.append(('game.text', message.game.text, message.game.text_entities or []))
    return fields


async def analyze_messages(messages, bot, temp_dir):
    ids = list(dict.fromkeys(entity.custom_emoji_id for message in messages for entity in message_text(message)[1] + [e for _, _, entities in additional_text_fields(message) for e in entities] if entity.type == 'custom_emoji' and entity.custom_emoji_id))
    ids = list(dict.fromkeys(ids + [node['custom_emoji_id'] for message in messages for _, node in walk_tree(rich_payload(message)) if node.get('type') == 'custom_emoji' and node.get('custom_emoji_id')]))
    stickers, warnings = {}, []
    for offset in range(0, len(ids), 200):
        try:
            found = await bot.get_custom_emoji_stickers(custom_emoji_ids=ids[offset:offset + 200])
            stickers.update({item.custom_emoji_id: item for item in found})
        except TelegramAPIError:
            warnings.append('Метаданные части custom emoji недоступны; использованы символы исходного текста.')
    packs = {}
    for sticker in stickers.values():
        if sticker.set_name:
            packs[sticker.set_name] = {'name': sticker.set_name, 'title': sticker.set_name, 'url': 'https://t.me/addemoji/' + sticker.set_name}
    for name, pack in packs.items():
        try:
            pack['title'] = (await bot.get_sticker_set(name)).title
        except TelegramAPIError:
            pass
    items = []
    for message in sorted(messages, key=lambda x: x.message_id):
        rich = rich_payload(message)
        text, entities = message_text(message)
        replaced, preserved, replacements = replace_custom_emoji(text, entities, stickers)
        media = await media_metadata(message, bot, temp_dir)
        additional = []
        for field, value, field_entities in additional_text_fields(message):
            new_value, new_entities, field_changes = replace_custom_emoji(value, field_entities, stickers)
            additional.append({'field': field, 'original_text': value, 'original_entities': [e.model_dump(mode='json', exclude_none=True) for e in field_entities], 'replaced_text': new_value, 'replaced_entities': [e.model_dump(mode='json', exclude_none=True) for e in new_entities], 'replacements': field_changes})
        if message.poll:
            warnings.append('Копия опроса создаётся как новый опрос; голоса исходного опроса не переносятся.')
        items.append({'message_id': message.message_id, 'type': 'rich_message' if rich is not None else message.content_type, 'media_group_id': message.media_group_id, 'forward_origin': message.forward_origin.model_dump(mode='json', exclude_none=True) if message.forward_origin else None, 'media': media, 'original_text': text, 'original_entities': [e.model_dump(mode='json', exclude_none=True) for e in entities or []], 'replaced_text': replaced, 'replaced_entities': [e.model_dump(mode='json', exclude_none=True) for e in preserved], 'replacements': replacements, 'structure': grid_structure(text, entities), 'additional_text_fields': additional, 'raw_message': message.model_dump(mode='json', exclude_none=True)})
        if rich is not None:
            items[-1].update(original_rich_message=rich, replaced_rich_message=replace_rich_emoji(rich, stickers), rich_structure=rich_structure(rich))
            grids = []
            for path, block in walk_tree(rich):
                if block.get('type') == 'paragraph':
                    assumed = grid_structure(*block_text(block))['assumed_grid']
                    if assumed:
                        grids.append({'path': path, **assumed})
            items[-1]['rich_structure']['emoji_grids'] = grids
    return {'schema_version': 1, 'messages': items, 'emoji_packs': list(packs.values()), 'custom_emoji_count': sum((sum(node.get('type') == 'custom_emoji' for _, node in walk_tree(x['original_rich_message'])) if 'original_rich_message' in x else len(x['replacements'])) + sum(len(f['replacements']) for f in x['additional_text_fields']) for x in items), 'unresolved_custom_emoji_ids': [x for x in ids if x not in stickers], 'limitations': list(dict.fromkeys(warnings + ['Размеры относятся к доступным файлам Telegram, а не к исходникам до загрузки.'])), 'copy_errors': []}


def report_text(analysis):
    lines = ['<b>Анализ поста</b>']
    labels = {'photo': 'Фото', 'video': 'Видео', 'animation': 'Анимация', 'document': 'Документ', 'audio': 'Аудио', 'voice': 'Голосовое', 'video_note': 'Кружок', 'sticker': 'Стикер', 'game_photo': 'Изображение игры', 'paid_photo': 'Платное фото', 'paid_video': 'Платное видео', 'paid_preview': 'Превью платного медиа'}
    for index, item in enumerate(analysis['messages'], 1):
        if len(analysis['messages']) > 1:
            lines.append(f'\n<b>Вложение / сообщение {index}</b>')
        if not item['media']:
            lines.append('Тип: ' + html.escape({'text': 'Текст', 'poll': 'Опрос', 'contact': 'Контакт', 'location': 'Геопозиция', 'venue': 'Место', 'dice': 'Игральный кубик', 'game': 'Игра'}.get(str(item['type']), str(item['type']))))
        for media in item['media']:
            lines.append('\n<b>' + labels.get(media['type'], media['type']) + '</b>')
            if media.get('width') and media.get('height'):
                lines.append(f"{media['width']} × {media['height']} px · {media['aspect_ratio']}")
            if 'duration' in media:
                lines.append(f"Длительность: {media['duration']} с")
            if media.get('file_size') is not None:
                lines.append(f"Размер файла: {media['file_size'] / 1024:.1f} КБ")
            if media.get('file_name'):
                lines.append(html.escape(media['file_name']))
            if media.get('probe_limitation'):
                lines.append(html.escape(media['probe_limitation']))
        if 'rich_structure' in item:
            structure = item['rich_structure']
            lines.append(f"\n<b>Блочный пост</b> · блоков: {structure['block_count']} · кнопок: {len(structure['buttons'])}")
            block_labels = {'paragraph': 'абзацы', 'heading': 'заголовки', 'pullquote': 'цитаты', 'video': 'видео', 'photo': 'фото', 'blockquote': 'цитаты', 'buttons': 'ряды кнопок'}
            lines.append(', '.join(f"{html.escape(block_labels.get(kind, kind))}: {count}" for kind, count in structure['block_types'].items()))
        grids = item.get('rich_structure', {}).get('emoji_grids', [])
        if not grids and item['structure']['assumed_grid']:
            grids = [item['structure']['assumed_grid']]
        for grid in grids:
            lines.append(f"Предполагаемая сетка TG Art: {grid['columns']}×{grid['rows']}")
    if analysis['custom_emoji_count']:
        lines.append(f"\n<b>Custom emoji</b>\nИспользовано: {analysis['custom_emoji_count']}")
        for pack in analysis['emoji_packs']:
            lines.append(f'<a href="{html.escape(pack["url"], quote=True)}">{html.escape(pack["title"])}</a>')
        if analysis['unresolved_custom_emoji_ids'] or not analysis['emoji_packs']:
            lines.append('Часть наборов определить не удалось.' if analysis['emoji_packs'] else 'Наборы определить не удалось.')
    for limitation in analysis['limitations']:
        if not limitation.startswith('Размеры относятся'):
            lines.append(html.escape(limitation))
    lines.append('\nРазмеры — для доступных файлов Telegram. Полная структура — в JSON.')
    return '\n'.join(lines)


def visible_whitespace(text: str) -> str:
    """HTML preview that exposes spacing without changing the copied post."""
    rendered = []
    for line in text.split('\n'):
        if line == '':
            rendered.append('<code>↵</code>')
            continue
        parts = []
        for character in line:
            if character == ' ':
                parts.append('<code>·</code>')
            elif character == '\u00a0':
                parts.append('<code>⍽</code>')
            else:
                parts.append(html.escape(character))
        rendered.append(''.join(parts))
    return '\n'.join(rendered)


async def send_replaced_text(bot, chat_id, text, entities=None, link_preview_options=None):
    """Split large outputs at Unicode boundaries and rebase formatting."""
    entities = entities or []
    start = 0
    while text:
        size, count = 0, 0
        for char in text:
            units = utf16_length(char)
            if size + units > 3500:
                break
            size += units
            count += 1
        chunk, text = text[:count], text[count:]
        clipped = []
        for entity in entities:
            left = max(start, entity.offset)
            right = min(start + size, entity.offset + entity.length)
            if left < right:
                clipped.append(entity.model_copy(update={'offset': left - start, 'length': right - left}))
        await bot.send_message(chat_id=chat_id, text=chunk, entities=clipped, parse_mode=None, link_preview_options=link_preview_options)
        start += size


async def copy_post(messages, analysis, bot, chat_id):
    by_id = {item['message_id']: item for item in analysis['messages']}
    groups = []
    for message in sorted(messages, key=lambda x: x.message_id):
        if message.media_group_id and groups and groups[-1][0].media_group_id == message.media_group_id:
            groups[-1].append(message)
        else:
            groups.append([message])
    for group in groups:
        overflow = []
        try:
            if len(group) > 1:
                media = []
                for message in group:
                    item = by_id[message.message_id]
                    caption = item['replaced_text']
                    if utf16_length(caption) > 1024:
                        overflow.append(item)
                        caption = ''
                    args = {'caption': caption, 'caption_entities': [MessageEntity.model_validate(e) for e in item['replaced_entities']] if caption else [], 'parse_mode': None}
                    if message.photo:
                        media.append(InputMediaPhoto(media=message.photo[-1].file_id, has_spoiler=message.has_media_spoiler or False, show_caption_above_media=getattr(message, 'show_caption_above_media', False), **args))
                    else:
                        for kind, cls in [('video', InputMediaVideo), ('audio', InputMediaAudio), ('document', InputMediaDocument)]:
                            attachment = getattr(message, kind, None)
                            if attachment:
                                if kind == 'video':
                                    args.update(has_spoiler=message.has_media_spoiler or False, show_caption_above_media=getattr(message, 'show_caption_above_media', False))
                                media.append(cls(media=attachment.file_id, **args))
                                break
                if len(media) != len(group):
                    raise ValueError('Тип части элементов альбома не поддерживает повторную отправку.')
                await bot.send_media_group(chat_id=chat_id, media=media)
            else:
                message = group[0]
                item = by_id[message.message_id]
                entities = [MessageEntity.model_validate(e) for e in item['replaced_entities']]
                if 'replaced_rich_message' in item:
                    await bot(SendRichMessage(chat_id=chat_id, rich_message=input_rich_message(item['replaced_rich_message'])))
                elif message.poll:
                    poll = message.poll
                    fields = {field['field']: field for field in item['additional_text_fields']}
                    if poll.type == 'quiz' and poll.correct_option_id is None:
                        raise ValueError('Нельзя воспроизвести quiz без известного правильного ответа.')
                    question = fields['poll.question']
                    options = []
                    for index in range(len(poll.options)):
                        field = fields[f'poll.options.{index}']
                        options.append(InputPollOption(text=field['replaced_text'], text_entities=[MessageEntity.model_validate(e) for e in field['replaced_entities']], text_parse_mode=None))
                    kwargs = {}
                    if 'poll.explanation' in fields:
                        field = fields['poll.explanation']
                        kwargs.update(explanation=field['replaced_text'], explanation_entities=[MessageEntity.model_validate(e) for e in field['replaced_entities']], explanation_parse_mode=None)
                    await bot.send_poll(chat_id=chat_id, question=question['replaced_text'], question_entities=[MessageEntity.model_validate(e) for e in question['replaced_entities']], question_parse_mode=None, options=options, is_anonymous=poll.is_anonymous, type=poll.type, allows_multiple_answers=poll.allows_multiple_answers, correct_option_id=poll.correct_option_id, is_closed=poll.is_closed, **kwargs)
                elif message.game and any(f['replacements'] for f in item['additional_text_fields']):
                    raise ValueError('Текст игры нельзя заменить при копировании; выведена текстовая версия.')
                elif message.text is not None:
                    await send_replaced_text(bot, chat_id, item['replaced_text'], entities, message.link_preview_options)
                else:
                    kwargs = {}
                    if message.caption is not None:
                        caption = item['replaced_text']
                        if utf16_length(caption) > 1024:
                            overflow.append(item)
                            caption = ''
                        kwargs.update(caption=caption, caption_entities=entities if caption else [], parse_mode=None, show_caption_above_media=getattr(message, 'show_caption_above_media', False))
                    await bot.copy_message(chat_id=chat_id, from_chat_id=message.chat.id, message_id=message.message_id, **kwargs)
            for item in overflow:
                await send_replaced_text(bot, chat_id, item['replaced_text'], [MessageEntity.model_validate(e) for e in item['replaced_entities']])
            if overflow:
                analysis['limitations'].append('Длинная подпись выведена отдельным текстом после медиа.')
        except (TelegramAPIError, ValueError) as error:
            analysis['copy_errors'].append({'message_ids': [m.message_id for m in group], 'error': str(error)[:500]})
            if len(group) == 1 and 'replaced_rich_message' in by_id[group[0].message_id]:
                item = by_id[group[0].message_id]
                await send_replaced_text(bot, chat_id, 'Telegram не позволил отправить блочную копию. Медиа и текст выведены отдельно; исходная структура сохранена в JSON.')
                for media in item['media']:
                    try:
                        kind = media['type']
                        await getattr(bot, 'send_' + kind)(chat_id=chat_id, **{kind: media['file_id']})
                    except (TelegramAPIError, AttributeError, KeyError) as media_error:
                        analysis['copy_errors'].append({'message_ids': [group[0].message_id], 'error': str(media_error)[:500], 'rich_block_path': media.get('rich_block_path')})
                if item['replaced_text'].strip():
                    await send_replaced_text(bot, chat_id, item['replaced_text'], [MessageEntity.model_validate(e) for e in item['replaced_entities']])
                continue
            text = '\n\n'.join(value for m in group for value in [by_id[m.message_id]['replaced_text']] + [f['replaced_text'] for f in by_id[m.message_id]['additional_text_fields']] if value).strip()
            await send_replaced_text(bot, chat_id, 'Не удалось воспроизвести этот тип сообщения полностью. Данные сохранены в JSON.' + ('\n\n' + text if text else ''))


async def send_analysis(messages, bot, chat_id, temp_dir):
    analysis = await analyze_messages(messages, bot, temp_dir)
    report = report_text(analysis)
    if len(report) > 3800:
        report = f"<b>Анализ поста</b>\nСообщений: {len(messages)}\nCustom emoji: {analysis['custom_emoji_count']}\nПодробные характеристики и список наборов — в JSON."
    await bot.send_message(chat_id=chat_id, text=report, parse_mode='HTML')
    for index, item in enumerate(analysis['messages'], start=1):
        source = item['original_text']
        if not source:
            continue
        preview = visible_whitespace(source)
        if len(preview) > 3500:
            preview = preview[:3500] + '\n…'
        await bot.send_message(
            chat_id=chat_id,
            text=f'<b>Текст {index}: служебные символы</b>\n{preview}',
            parse_mode='HTML',
        )
    await copy_post(messages, analysis, bot, chat_id)
    payload = json.dumps(analysis, ensure_ascii=False, indent=2).encode('utf-8')
    await bot.send_document(chat_id=chat_id, document=BufferedInputFile(payload, filename='post-analysis.json'))
    return analysis
