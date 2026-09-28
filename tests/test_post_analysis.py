import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import CopyMessage, GetCustomEmojiStickers
from aiogram.types import Message, MessageEntity
from app.post_analysis import replace_custom_emoji, grid_structure, analyze_messages, copy_post, send_analysis, media_metadata, send_replaced_text


def message(mid=1, **content):
    return Message.model_validate({'message_id': mid, 'date': 0, 'chat': {'id': 3, 'type': 'private'}, 'from': {'id': 3, 'is_bot': False, 'first_name': 'Test'}, **content})


def emoji(offset, length=2, cid='1'):
    return MessageEntity(type='custom_emoji', offset=offset, length=length, custom_emoji_id=cid)


class TextAnalysisTest(unittest.TestCase):
    def test_utf16_replacement_preserves_containing_and_following_formatting(self):
        text = '😀 🎨 link'
        entities = [emoji(3), MessageEntity(type='bold', offset=0, length=5), MessageEntity(type='text_link', offset=6, length=4, url='https://example.com')]
        replaced, preserved, changes = replace_custom_emoji(text, entities, {'1': SimpleNamespace(emoji='❤️')})
        self.assertEqual(replaced, '😀 ❤️ link')
        self.assertEqual([(e.type, e.offset, e.length) for e in preserved], [('bold', 0, 5), ('text_link', 6, 4)])
        replaced, preserved, changes = replace_custom_emoji(text, entities, {'1': SimpleNamespace(emoji='👨‍👩‍👧‍👦')})
        self.assertEqual(preserved[0].length, 14)
        self.assertEqual(preserved[1].offset, 15)
        self.assertEqual(preserved[1].url, 'https://example.com')
        self.assertEqual(changes[0]['original'], '🎨')

    def test_unavailable_emoji_uses_original_symbol(self):
        replaced, entities, _ = replace_custom_emoji('🎨', [emoji(0)], {})
        self.assertEqual(replaced, '🎨')
        self.assertEqual(entities, [])

    def test_multiple_replacements_adjust_lengths(self):
        text, entities, changes = replace_custom_emoji('🎨🎨x', [emoji(0), emoji(2), MessageEntity(type='italic', offset=4, length=1)], {'1': SimpleNamespace(emoji='a')})
        self.assertEqual(text, 'aax')
        self.assertEqual(entities[0].offset, 2)
        self.assertEqual(len(changes), 2)

    def test_grid_requires_contiguous_equal_rows(self):
        grid = grid_structure('🎨🎨\n🎨🎨', [emoji(0), emoji(2), emoji(5), emoji(7)])
        self.assertEqual(grid['assumed_grid'], {'columns': 2, 'rows': 2})
        self.assertIsNone(grid_structure('🎨\n\n🎨', [emoji(0), emoji(4)])['assumed_grid'])


class PostAnalysisTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.bot = AsyncMock()
        self.bot.get_custom_emoji_stickers.return_value = [SimpleNamespace(custom_emoji_id='1', emoji='😀', set_name='art')]
        self.bot.get_sticker_set.return_value = SimpleNamespace(title='Test Art')

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_three_outputs_and_json(self):
        original = message(text='🎨', entities=[emoji(0)])
        result = await send_analysis([original], self.bot, 3, Path(self.directory.name))
        self.assertEqual(result['custom_emoji_count'], 1)
        self.assertEqual(result['emoji_packs'][0]['title'], 'Test Art')
        self.assertEqual(self.bot.send_message.await_count, 3)
        self.assertIn('служебные символы', self.bot.send_message.await_args_list[1].kwargs['text'])
        self.assertEqual(self.bot.send_message.await_args_list[2].kwargs['text'], '😀')
        self.assertEqual(self.bot.send_message.await_args_list[2].kwargs['entities'], [])
        payload = self.bot.send_document.await_args.kwargs['document']
        document = json.loads(payload.data)
        self.assertEqual(document['schema_version'], 1)
        self.assertEqual(document['messages'][0]['raw_message']['text'], '🎨')
        self.assertEqual(document['messages'][0]['replaced_text'], '😀')
        self.assertEqual([call[0] for call in self.bot.mock_calls if call[0].startswith('send_')], ['send_message', 'send_message', 'send_message', 'send_document'])

    async def test_photo_caption_dimensions_and_copy(self):
        original = message(photo=[{'file_id': 'small', 'file_unique_id': 's', 'width': 100, 'height': 100}, {'file_id': 'large', 'file_unique_id': 'l', 'width': 1280, 'height': 720, 'file_size': 10000}], caption='🎨', caption_entities=[emoji(0)])
        analysis = await send_analysis([original], self.bot, 3, Path(self.directory.name))
        media = analysis['messages'][0]['media'][0]
        self.assertEqual((media['width'], media['height'], media['aspect_ratio']), (1280, 720, '16:9'))
        self.assertEqual(self.bot.copy_message.await_args.kwargs['caption'], '😀')
        self.assertEqual(self.bot.copy_message.await_args.kwargs['caption_entities'], [])
        self.bot.download.assert_not_awaited()

    async def test_album_order_and_caption_entities(self):
        originals = [message(mid=2, media_group_id='album', photo=[{'file_id': 'two', 'file_unique_id': '2', 'width': 200, 'height': 100}]), message(mid=1, media_group_id='album', photo=[{'file_id': 'one', 'file_unique_id': '1', 'width': 200, 'height': 100}], caption='🎨', caption_entities=[emoji(0)])]
        analysis = await send_analysis(originals, self.bot, 3, Path(self.directory.name))
        media = self.bot.send_media_group.await_args.kwargs['media']
        self.assertEqual([x.media for x in media], ['one', 'two'])
        self.assertEqual(media[0].caption, '😀')
        self.assertEqual(media[0].caption_entities, [])
        self.assertEqual([m['message_id'] for m in analysis['messages']], [1, 2])

    async def test_unavailable_metadata_falls_back_and_is_recorded(self):
        self.bot.get_custom_emoji_stickers.side_effect = TelegramBadRequest(method=GetCustomEmojiStickers(custom_emoji_ids=['1']), message='Unavailable')
        analysis = await send_analysis([message(text='🎨', entities=[emoji(0)])], self.bot, 3, Path(self.directory.name))
        self.assertEqual(analysis['messages'][0]['replaced_text'], '🎨')
        self.assertEqual(analysis['unresolved_custom_emoji_ids'], ['1'])
        self.assertTrue(any('Метаданные' in x for x in analysis['limitations']))

    async def test_unreproducible_service_message_still_exports_json(self):
        self.bot.copy_message.side_effect = TelegramBadRequest(method=CopyMessage(chat_id=3, from_chat_id=3, message_id=1), message='Service message cannot be copied')
        analysis = await send_analysis([message(new_chat_title='Example')], self.bot, 3, Path(self.directory.name))
        self.assertEqual(len(analysis['copy_errors']), 1)
        self.bot.send_document.assert_awaited_once()

    async def test_video_and_document_metadata(self):
        media = await media_metadata(message(video={'file_id': 'v', 'file_unique_id': 'v', 'width': 1920, 'height': 1080, 'duration': 12}), self.bot, Path(self.directory.name))
        self.assertEqual(media[0]['duration'], 12)
        self.assertEqual(media[0]['aspect_ratio'], '16:9')
        with patch('app.post_analysis.probe_video', new_callable=AsyncMock, return_value=SimpleNamespace(width=512, height=256, codec='png', pixel_format='rgba', duration=0)):
            media = await media_metadata(message(document={'file_id': 'd', 'file_unique_id': 'd', 'file_name': 'image.png', 'mime_type': 'image/png', 'file_size': 100}), self.bot, Path(self.directory.name))
        self.assertEqual(media[0]['width'], 512)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    async def test_poll_custom_emoji_is_replaced(self):
        poll = {'id': 'poll', 'question': '🎨?', 'question_entities': [emoji(0).model_dump()], 'options': [{'text': '🎨', 'voter_count': 0, 'text_entities': [emoji(0).model_dump()]}, {'text': 'Нет', 'voter_count': 0}], 'total_voter_count': 0, 'is_closed': False, 'is_anonymous': True, 'type': 'regular', 'allows_multiple_answers': False}
        analysis = await send_analysis([message(poll=poll)], self.bot, 3, Path(self.directory.name))
        self.assertEqual(analysis['custom_emoji_count'], 2)
        kwargs = self.bot.send_poll.await_args.kwargs
        self.assertEqual(kwargs['question'], '😀?')
        self.assertEqual(kwargs['options'][0].text, '😀')
        self.assertEqual(kwargs['options'][0].text_entities, [])

    async def test_large_text_splits_without_breaking_surrogates(self):
        text = '😀' * 2000
        await send_replaced_text(self.bot, 3, text, [MessageEntity(type='bold', offset=0, length=4000)])
        calls = self.bot.send_message.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(''.join(c.kwargs['text'] for c in calls), text)
        self.assertEqual(calls[0].kwargs['entities'][0].length, 3500)
        self.assertEqual(calls[1].kwargs['entities'][0].offset, 0)
        self.assertEqual(calls[1].kwargs['entities'][0].length, 500)

    async def test_large_document_not_downloaded(self):
        media = await media_metadata(message(document={'file_id': 'd', 'file_unique_id': 'd', 'file_name': 'image.png', 'file_size': 21 * 1024 * 1024}), self.bot, Path(self.directory.name))
        self.bot.download.assert_not_awaited()
        self.assertIn('probe_limitation', media[0])
