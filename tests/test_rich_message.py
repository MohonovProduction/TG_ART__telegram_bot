import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from app.rich_message import SendRichMessage, flatten_rich_message, replace_rich_emoji, input_rich_message, walk_tree
from app.post_analysis import analyze_messages, send_analysis, report_text, utf16_slice

FIXTURE = Path(__file__).parent / 'fixtures' / 'rich_post.json'


def message(rich):
    return Message.model_validate({'message_id': 1, 'date': 0, 'chat': {'id': 3, 'type': 'private'}, 'rich_message': rich})


class RichPostTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rich = json.loads(FIXTURE.read_text())
        self.directory = tempfile.TemporaryDirectory()
        self.bot = AsyncMock()
        ids = {node['custom_emoji_id'] for _, node in walk_tree(self.rich) if node.get('type') == 'custom_emoji'}
        self.bot.get_custom_emoji_stickers.return_value = [SimpleNamespace(custom_emoji_id=cid, emoji='😀', set_name='test_art') for cid in ids]
        self.bot.get_sticker_set.return_value = SimpleNamespace(title='Test Art')

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_user_sample_media_emoji_and_blocks_are_detected(self):
        analysis = await analyze_messages([message(self.rich)], self.bot, Path(self.directory.name))
        item = analysis['messages'][0]
        self.assertEqual(item['type'], 'rich_message')
        self.assertEqual(analysis['custom_emoji_count'], 29)
        media = item['media'][0]
        self.assertEqual((media['width'], media['height'], media['duration'], media['aspect_ratio']), (1920, 1440, 5, '4:3'))
        self.assertEqual(item['rich_structure']['block_count'], 9)
        self.assertEqual(len(item['rich_structure']['buttons']), 3)
        self.assertEqual(item['rich_structure']['emoji_grids'][0]['columns'], 3)
        self.assertEqual(item['rich_structure']['emoji_grids'][0]['rows'], 9)
        self.assertEqual(len(item['replacements']), 29)
        report = report_text(analysis)
        self.assertIn('1920 × 1440', report)
        self.assertIn('3×9', report)
        self.assertIn('Test Art', report)
        self.bot.download.assert_not_awaited()

    async def test_rich_copy_keeps_layout_and_buttons_with_no_custom_emoji(self):
        result = await send_analysis([message(self.rich)], self.bot, 3, Path(self.directory.name))
        request = self.bot.call_args.args[0]
        self.assertIsInstance(request, SendRichMessage)
        self.assertEqual(request.__api_method__, 'sendRichMessage')
        self.assertEqual(request.rich_message['blocks'][0]['video']['media'], 'test-rich-video')
        self.assertEqual(request.rich_message['blocks'][0]['video']['type'], 'video')
        self.assertNotIn('file_id', request.rich_message['blocks'][0]['video'])
        self.assertEqual(request.rich_message['blocks'][2]['type'], 'pullquote')
        self.assertEqual(request.rich_message['blocks'][3]['size'], 2)
        self.assertFalse(any(node.get('type') == 'custom_emoji' for _, node in walk_tree(request.rich_message)))
        buttons = [node['button'] for _, node in walk_tree(request.rich_message) if node.get('type') == 'button']
        self.assertEqual([x['style'] for x in buttons], ['primary', 'danger', 'success'])
        self.assertTrue(all(x['url'] for x in buttons))
        self.assertEqual(result['messages'][0]['original_rich_message'], self.rich)
        self.assertEqual([call[0] for call in self.bot.mock_calls if call[0] in ('send_message', '', 'send_document')], ['send_message', '', 'send_document'])
        self.assertEqual(json.loads(self.bot.send_document.call_args.kwargs['document'].data)['custom_emoji_count'], 29)

    async def test_api_rejection_falls_back_to_media_and_formatted_text(self):
        self.bot.side_effect = TelegramBadRequest(method=SendRichMessage(chat_id=3, rich_message={'blocks': []}), message='Rich messages unavailable')
        result = await send_analysis([message(self.rich)], self.bot, 3, Path(self.directory.name))
        self.bot.send_video.assert_awaited_once_with(chat_id=3, video='test-rich-video')
        self.assertTrue(result['copy_errors'])
        text = self.bot.send_message.await_args_list[-1].kwargs
        self.assertTrue(text['entities'])
        self.assertFalse(any(e.type == 'custom_emoji' for e in text['entities']))
        self.bot.send_document.assert_awaited_once()

    async def test_nested_photo_and_caption_credit(self):
        rich = {'blocks': [{'type': 'collage', 'blocks': [{'type': 'photo', 'photo': [{'file_id': 'small', 'file_unique_id': 's', 'width': 10, 'height': 10}, {'file_id': 'large', 'file_unique_id': 'l', 'width': 800, 'height': 600}], 'has_spoiler': True, 'caption': {'text': {'type': 'custom_emoji', 'custom_emoji_id': '1', 'alternative_text': '👀'}, 'credit': {'type': 'bold', 'text': 'Author'}}}]}]}
        analysis = await analyze_messages([message(rich)], self.bot, Path(self.directory.name))
        self.assertEqual(analysis['messages'][0]['media'][0]['width'], 800)
        outgoing = input_rich_message(analysis['messages'][0]['replaced_rich_message'])
        photo = outgoing['blocks'][0]['blocks'][0]
        self.assertEqual(photo['photo']['media'], 'large')
        self.assertTrue(photo['photo']['has_spoiler'])
        self.assertEqual(photo['caption']['credit']['text'], 'Author')


class RichConversionTest(unittest.TestCase):
    def test_flatten_nested_formatting_has_valid_utf16_offsets(self):
        rich = {'blocks': [{'type': 'paragraph', 'text': ['😀', {'type': 'bold', 'text': {'type': 'url', 'url': 'https://example.com', 'text': {'type': 'custom_emoji', 'custom_emoji_id': '1', 'alternative_text': '👀'}}}]}]}
        text, entities = flatten_rich_message(rich)
        self.assertEqual(text, '😀👀')
        self.assertEqual({e.type for e in entities}, {'bold', 'text_link', 'custom_emoji'})
        for entity in entities:
            self.assertEqual(entity.offset, 2)
            self.assertEqual(utf16_slice(text, entity.offset, entity.length), '👀')
        replaced = replace_rich_emoji(rich, {})
        self.assertEqual(replaced['blocks'][0]['text'][1]['text']['text'], '👀')
        self.assertEqual(rich['blocks'][0]['text'][1]['text']['text']['type'], 'custom_emoji')

    def test_serialization_through_current_aiogram(self):
        bot = Bot('123456:FAKE_TOKEN_FOR_TESTS')
        request = SendRichMessage(chat_id=3, rich_message=input_rich_message({'blocks': [{'type': 'video', 'video': {'file_id': 'video', 'width': 1920, 'height': 1440, 'duration': 5}}]}))
        encoded = bot.session.prepare_value(request.rich_message, bot=bot, files={})
        self.assertEqual(json.loads(encoded)['blocks'][0]['video']['media'], 'video')
