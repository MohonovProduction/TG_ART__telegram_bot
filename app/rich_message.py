"""Compatibility with rich posts while using aiogram 3.20.

Incoming unknown fields are retained by aiogram. Outgoing blocks must use
InputMedia objects, rather than the file metadata returned by Telegram.
"""
from collections import Counter
from typing import ClassVar, Any

from aiogram.methods.base import TelegramMethod
from aiogram.types import Message, MessageEntity
from app.telegram_serialization import telegram_model_dump


class SendRichMessage(TelegramMethod[Message]):
    __api_method__: ClassVar[str] = 'sendRichMessage'
    __returning__: ClassVar[type] = Message
    chat_id: int | str
    rich_message: dict[str, Any]


MEDIA_TYPES = ('photo', 'video', 'animation', 'audio', 'document', 'voice_note')


def rich_payload(message):
    value = getattr(message, 'rich_message', None)
    if hasattr(value, 'model_dump'):
        value = telegram_model_dump(value)
    return value if isinstance(value, dict) else None


def walk_tree(value, path='rich_message'):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from walk_tree(child, f'{path}.{key}')
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_tree(child, f'{path}.{index}')


def text_value(value):
    """Flatten RichText only; never include metadata such as URLs in its text."""
    if isinstance(value, str):
        return value, []
    if isinstance(value, list):
        return join_text([text_value(child) for child in value], '')
    if not isinstance(value, dict):
        return '', []
    kind = value.get('type')
    if kind == 'custom_emoji':
        text = value.get('alternative_text') or '▫️'
        return text, [MessageEntity(type='custom_emoji', offset=0, length=units(text), custom_emoji_id=value['custom_emoji_id'])]
    if kind == 'button':
        button = value.get('button', {})
        text, entities = text_value(button.get('text', ''))
        if text and button.get('url'):
            entities.append(MessageEntity(type='text_link', offset=0, length=units(text), url=button['url']))
        return text, entities
    text, entities = text_value(value.get('text', value.get('expression', '')))
    if not text:
        return text, entities
    if kind in ('bold', 'italic', 'underline', 'strikethrough', 'spoiler', 'code'):
        entities.append(MessageEntity(type=kind, offset=0, length=units(text)))
    elif kind == 'url' and value.get('url'):
        entities.append(MessageEntity(type='text_link', offset=0, length=units(text), url=value['url']))
    elif kind == 'text_mention' and value.get('user'):
        entities.append(MessageEntity(type='text_mention', offset=0, length=units(text), user=value['user']))
    return text, entities


def units(text):
    return len(text.encode('utf-16-le')) // 2


def join_text(parts, separator):
    text, entities = '', []
    for index, (value, formatting) in enumerate(parts):
        if index:
            text += separator
        offset = units(text)
        entities.extend(entity.model_copy(update={'offset': entity.offset + offset}) for entity in formatting)
        text += value
    return text, entities


def block_text(block):
    kind = block.get('type')
    parts = []
    if 'summary' in block:
        parts.append(text_value(block['summary']))
    if 'text' in block:
        text, entities = text_value(block['text'])
        if text and kind in ('heading', 'pre', 'pullquote', 'expandable_blockquote'):
            entity_type = {'heading': 'bold', 'pre': 'pre', 'pullquote': 'blockquote', 'expandable_blockquote': 'expandable_blockquote'}[kind]
            entities.append(MessageEntity(type=entity_type, offset=0, length=units(text)))
        parts.append((text, entities))
    if 'blocks' in block:
        parts.extend(block_text(child) for child in block['blocks'])
    if kind == 'list':
        for item in block.get('items', []):
            parts.append(join_text([block_text(child) for child in item.get('blocks', [])], '\n'))
    if kind == 'table':
        for row in block.get('cells', []):
            parts.append(join_text([text_value(cell.get('text', '')) for cell in row], '\t'))
    if kind == 'buttons':
        parts.append(join_text([text_value({'type': 'button', 'button': button}) for button in block.get('buttons', [])], ' '))
    caption = block.get('caption')
    if caption is not None:
        if isinstance(caption, dict) and 'type' not in caption:
            parts.append(text_value(caption.get('text', '')))
            if caption.get('credit'):
                parts.append(text_value(caption['credit']))
        else:
            parts.append(text_value(caption))
    if 'credit' in block:
        parts.append(text_value(block['credit']))
    return join_text(parts, '\n')


def flatten_rich_message(rich):
    # Keep empty paragraphs; skip media blocks with no captions.
    parts = [block_text(block) for block in rich.get('blocks', [])]
    return join_text([part for block, part in zip(rich.get('blocks', []), parts) if part[0] or block.get('type') == 'paragraph'], '\n\n')


def replace_rich_emoji(value, stickers):
    if isinstance(value, dict):
        if value.get('type') == 'custom_emoji':
            sticker = stickers.get(value.get('custom_emoji_id'))
            return getattr(sticker, 'emoji', None) or value.get('alternative_text') or '▫️'
        return {key: replace_rich_emoji(child, stickers) for key, child in value.items()}
    if isinstance(value, list):
        return [replace_rich_emoji(child, stickers) for child in value]
    return value


def input_rich_message(rich):
    """Recursively translate received RichBlock media to InputRichBlock media."""
    def convert(value):
        if isinstance(value, list):
            return [convert(child) for child in value]
        if not isinstance(value, dict):
            return value
        result = {key: convert(child) for key, child in value.items()}
        kind = value.get('type')
        if kind in MEDIA_TYPES and kind in value:
            media = value[kind]
            if isinstance(media, list):
                media = max(media, key=lambda item: item.get('width', 0) * item.get('height', 0))
            if not isinstance(media, dict) or not media.get('file_id'):
                raise ValueError(f'Нет file_id у блока {kind}.')
            outgoing = {'type': kind, 'media': media['file_id']}
            for key in ('width', 'height', 'duration', 'performer', 'title'):
                if key in media and kind in ('video', 'animation', 'audio', 'voice_note'):
                    outgoing[key] = media[key]
            if kind == 'video' and 'supports_streaming' in media:
                outgoing['supports_streaming'] = media['supports_streaming']
            if value.get('has_spoiler'):
                outgoing['has_spoiler'] = True
            result.pop('has_spoiler', None)
            result[kind] = outgoing
        # Incoming list labels are generated by Telegram, not InputRichBlock fields.
        if kind == 'list':
            for item in result.get('items', []):
                item.pop('label', None)
        return result
    converted = convert(rich)
    converted['skip_entity_detection'] = True
    return converted


def rich_structure(rich):
    blocks, buttons = [], []
    for path, node in walk_tree(rich):
        if path.rsplit('.', 2)[-2:-1] == ['blocks'] and path.rsplit('.', 1)[-1].isdigit():
            blocks.append({'path': path, 'type': node.get('type'), **({'size': node['size']} if 'size' in node else {})})
        if node.get('type') == 'button':
            button = node.get('button', {})
            buttons.append({'path': path, 'text': text_value(button.get('text', ''))[0], 'style': button.get('style'), 'url': button.get('url')})
        elif node.get('type') == 'buttons':
            for index, button in enumerate(node.get('buttons', [])):
                buttons.append({'path': f'{path}.buttons.{index}', 'text': text_value(button.get('text', ''))[0], 'style': button.get('style'), 'url': button.get('url')})
    return {'block_count': len(blocks), 'block_types': dict(Counter(block['type'] for block in blocks)), 'blocks': blocks, 'buttons': buttons}
