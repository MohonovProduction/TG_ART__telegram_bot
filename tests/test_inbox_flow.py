import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from app import access, bot, inbox_flow as inbox, video_note_flow as note
from app.config import Settings
from app.renderer import RenderError


def incoming(mid=1, actor=3, **content):
    return Message.model_validate({'message_id': mid, 'date': 0, 'chat': {'id': actor, 'type': 'private'}, 'from': {'id': actor, 'is_bot': False, 'first_name': 'Test'}, **content})


def photo(mid=1, **kwargs):
    return incoming(mid, photo=[{'file_id': f'photo-{mid}', 'file_unique_id': str(mid), 'width': 100, 'height': 100}], **kwargs)


def video(mid=1, **kwargs):
    return incoming(mid, video={'file_id': f'video-{mid}', 'file_unique_id': str(mid), 'width': 100, 'height': 100, 'duration': 1}, **kwargs)


class InboxTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.old_store, self.old_settings, self.old_config = access.store, getattr(bot, 'settings', None), inbox.config
        access.store = access.AccessStore(self.root / 'access.db', 1)
        access.store.change(1, 3, 'user')
        access.store.change(1, 2, 'admin')
        bot.settings = Settings('fake', 1, self.root, self.root, self.root)
        bot.configure_interactions(bot.settings)
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=3, user_id=3))
        self.telegram = AsyncMock()
        async def download(file, destination):
            Path(destination).write_bytes(b'fake-source')
        self.telegram.download.side_effect = download
        self.handler = AsyncMock()
        self.middleware = inbox.ContentFirstMiddleware()
        self.answers = patch.object(Message, 'answer', new_callable=AsyncMock)
        self.answer = self.answers.start()

    async def asyncTearDown(self):
        self.answers.stop()
        access.store.db.close()
        access.store, bot.settings, inbox.config = self.old_store, self.old_settings, self.old_config
        self.directory.cleanup()

    async def receive(self, message):
        await self.middleware(self.handler, message, {'state': self.state, 'bot': self.telegram})

    async def test_photo_action_uses_received_file_and_waits_for_grid(self):
        await self.receive(photo())
        self.telegram.download.assert_not_awaited()
        self.assertEqual(await self.state.get_state(), inbox.InboxFlow.choosing.state)
        await inbox.choose_action(incoming(text=inbox.ART), self.state, self.telegram)
        self.assertEqual(await self.state.get_state(), bot.RenderFlow.waiting_for_grid.state)
        self.telegram.download.assert_awaited_once()
        data = await self.state.get_data()
        self.assertTrue(Path(data['source']).exists())
        await bot._reset_flow(self.state)
        self.assertFalse(Path(data['source']).exists())

    async def test_switch_continue_restores_data_and_new_cleans_old_file(self):
        path = self.root / 'old.png'
        path.write_bytes(b'old')
        original = {'source': str(path), 'temporary': True, 'pending_grid': [7, 5]}
        await self.state.set_state(bot.RenderFlow.waiting_for_grid)
        await self.state.set_data(original)
        await self.receive(video())
        self.assertEqual(await self.state.get_state(), inbox.InboxFlow.switching.state)
        self.assertTrue(path.exists())
        await inbox.choose_replacement(incoming(text=inbox.CONTINUE), self.state)
        self.assertEqual(await self.state.get_data(), original)
        self.assertEqual(await self.state.get_state(), bot.RenderFlow.waiting_for_grid.state)
        await self.receive(video(2))
        await inbox.choose_replacement(incoming(text=inbox.NEW), self.state)
        self.assertFalse(path.exists())
        self.assertEqual(await self.state.get_state(), inbox.InboxFlow.choosing.state)
        self.assertEqual(inbox.stored_messages(await self.state.get_data())[0].message_id, 2)

    async def test_cancel_cleans_suspended_sources(self):
        path = self.root / 'old.mp4'
        path.write_bytes(b'old')
        await self.state.set_state(note.VideoNoteFlow.mode)
        await self.state.set_data({'video_note_source': str(path), 'video_note_temporary': True})
        await self.receive(photo())
        await bot._reset_flow(self.state)
        self.assertFalse(path.exists())
        self.assertIsNone(await self.state.get_state())

    async def test_album_uses_all_files_in_sticker_pack(self):
        await self.receive(photo(1, media_group_id='album'))
        await self.receive(photo(2, media_group_id='album'))
        messages = inbox.stored_messages(await self.state.get_data())
        self.assertNotIn(inbox.ART, inbox.available_actions(messages, 3))
        await inbox.choose_action(incoming(text=inbox.STICKERS), self.state, self.telegram)
        data = await self.state.get_data()
        self.assertEqual(len(data['sticker_sources']), 2)
        self.assertEqual(await self.state.get_state(), bot.RenderFlow.collecting_stickers.state)
        await self.receive(photo(3))
        self.assertEqual(len((await self.state.get_data())['sticker_sources']), 3)
        await inbox.finish_stickers(incoming(text=inbox.DONE), self.state)
        self.assertEqual(await self.state.get_state(), bot.RenderFlow.waiting_for_sticker_emoji_mode.state)

    async def test_mixed_album_selects_pack_kind_and_reuses_matching_files(self):
        await self.receive(photo(1, media_group_id='mixed'))
        await self.receive(video(2, media_group_id='mixed'))
        await inbox.choose_action(incoming(text=inbox.STICKERS), self.state, self.telegram)
        self.assertEqual(await self.state.get_state(), inbox.InboxFlow.sticker_kind.state)
        self.telegram.download.assert_not_awaited()
        await inbox.choose_sticker_kind(incoming(text='Видео'), self.state, self.telegram)
        self.assertEqual((await self.state.get_data())['sticker_format'], 'video')
        self.assertEqual(len((await self.state.get_data())['sticker_sources']), 1)
        self.telegram.download.assert_awaited_once()

    async def test_wrong_kind_during_collection_prompts_explicit_switch(self):
        await self.state.set_state(bot.RenderFlow.collecting_stickers)
        await self.state.set_data({'sticker_format': 'static', 'sticker_sources': []})
        await self.receive(video())
        self.assertEqual(await self.state.get_state(), inbox.InboxFlow.switching.state)

    async def test_expected_source_and_analysis_inputs_use_current_flow(self):
        for current in (bot.RenderFlow.waiting_for_source, 'AnalysisFlow:collecting'):
            await self.state.set_state(current)
            await self.receive(photo())
        self.assertEqual(self.handler.await_count, 2)

    async def test_contact_roles_respect_actor_and_protected_targets(self):
        contact = incoming(actor=1, contact={'phone_number': '123', 'first_name': 'Person', 'user_id': 9})
        await self.receive(contact)
        self.assertIn(inbox.ADMIN, inbox.available_actions([contact], 1))
        self.assertNotIn(inbox.ADMIN, inbox.available_actions([contact], 2))
        self.assertEqual(inbox.available_actions([contact], 3), [inbox.ANALYZE])
        await inbox.choose_action(incoming(actor=1, text=inbox.ADMIN), self.state, self.telegram)
        self.assertEqual(access.store.role(9), 'admin')
        self.assertEqual(inbox.available_actions([contact], 2), [inbox.ANALYZE])
        own = incoming(contact={'phone_number': '123', 'first_name': 'Owner', 'user_id': 1})
        self.assertEqual(inbox.available_actions([own], 1), [inbox.ANALYZE])

    async def test_video_note_reuses_upload_and_offers_settings(self):
        await self.receive(video())
        await inbox.choose_action(incoming(text=inbox.NOTE), self.state, self.telegram)
        self.assertEqual(await self.state.get_state(), note.VideoNoteFlow.mode.state)
        self.telegram.download.assert_awaited_once()

    async def test_non_alpha_art_starts_grid_rendering(self):
        await self.receive(video())
        await inbox.choose_action(incoming(text=inbox.ART), self.state, self.telegram)
        self.assertEqual(await self.state.get_state(), 'RenderFlow:waiting_for_grid')
        self.assertTrue(list(self.root.glob('*.mp4')))

    async def test_forwarded_text_analysis_uses_original_message(self):
        original = incoming(text='Post', forward_origin={'type': 'hidden_user', 'date': 0, 'sender_user_name': 'Sender'})
        await self.receive(original)
        with patch('app.inbox_flow.send_analysis', new_callable=AsyncMock) as analyze:
            await inbox.choose_action(incoming(text=inbox.ANALYZE), self.state, self.telegram)
        self.assertEqual(analyze.call_args.args[0][0].text, 'Post')
        self.assertIsNone(await self.state.get_state())

    def test_extension_only_document_and_rich_video_detection(self):
        document = incoming(document={'file_id': 'doc', 'file_unique_id': 'd', 'file_name': 'file.png'})
        self.assertEqual(inbox.media_sources(document)[0]['kind'], 'image')
        rich = incoming(rich_message={'blocks': [{'type': 'video', 'video': {'file_id': 'v', 'file_unique_id': 'v', 'width': 10, 'height': 10, 'duration': 1}}]})
        self.assertIn(inbox.NOTE, inbox.available_actions([rich], 3))


class VideoNoteSettingsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=3, user_id=3))
        self.old_render = note.render
        note.render = AsyncMock()
        self.answers = patch.object(Message, 'answer', new_callable=AsyncMock)
        self.answers.start()
        await note.start_options(incoming(), self.state, Path('/tmp/test-video.mp4'), True)

    async def asyncTearDown(self):
        note.render = self.old_render
        self.answers.stop()

    async def test_fit_hex_invalid_input_back_and_valid_color(self):
        await note.choose_mode(incoming(text='↔️ Fit — вписать'), self.state)
        await note.choose_background(incoming(text=note.HEX), self.state)
        await note.receive_hex(incoming(text='not-a-color'), self.state)
        note.render.assert_not_awaited()
        self.assertEqual(await self.state.get_state(), note.VideoNoteFlow.hex_color.state)
        await note.receive_hex(incoming(text=note.BACK), self.state)
        self.assertEqual(await self.state.get_state(), note.VideoNoteFlow.background.state)
        await note.choose_background(incoming(text=note.HEX), self.state)
        await note.receive_hex(incoming(text='#242424'), self.state)
        self.assertEqual(note.render.call_args.kwargs, {'mode': 'fit', 'background': '#242424'})

    async def test_cover_and_fill_start_only_after_required_choices(self):
        await note.choose_mode(incoming(text='✂️ Cover — обрезать'), self.state)
        note.render.assert_not_awaited()
        await note.choose_position(incoming(text='⬅️ Лево'), self.state)
        self.assertEqual(note.render.call_args.kwargs, {'mode': 'cover', 'position': 'left'})
        await note.start_options(incoming(), self.state, Path('/tmp/test-video.mp4'), True)
        await note.choose_mode(incoming(text='🔲 Fill — растянуть'), self.state)
        self.assertEqual(note.render.call_args.kwargs, {'mode': 'fill'})

    async def test_fit_blur_starts_immediately(self):
        await note.choose_mode(incoming(text='↔️ Fit — вписать'), self.state)
        await note.choose_background(incoming(text='🌫 Размытое видео'), self.state)
        self.assertEqual(note.render.call_args.kwargs, {'mode': 'fit', 'background': 'blur'})
