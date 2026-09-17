import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetStickerSet
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app import access, admin, bot as flows, analysis_flow
from app.access import AccessStore, AccessMiddleware
from app.grid_keyboard import grid_keyboard, parse_grid, confirmed_grid, CONFIRM, CHANGE
from app.pack_names import available_pack_name
from app.sticker_pack import suggest_pack_name, make_sticker_set_name


class RolesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'access.db'
        self.store = AccessStore(self.path, 1)

    def tearDown(self):
        self.store.db.close()
        self.directory.cleanup()

    def test_hierarchy_and_revocation(self):
        self.store.change(1, 2, 'admin')
        self.store.change(2, 3, 'user')
        for actor, target, role in [(2, 4, 'admin'), (2, 2, None), (3, 4, 'user'), (1, 1, None), (1, 0, 'user')]:
            with self.assertRaises(ValueError):
                self.store.change(actor, target, role)
        self.store.change(2, 3, None)
        self.assertIsNone(self.store.role(3))
        self.assertEqual(self.store.role(1), 'owner')

    def test_persistence(self):
        self.store.change(1, 3, 'user')
        second = AccessStore(self.path, 1)
        self.assertEqual(second.role(3), 'user')
        second.db.close()


class FlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=3, user_id=3))
        self.message = SimpleNamespace(text='□7×5', from_user=SimpleNamespace(id=3), answer=AsyncMock(), contact=None)
        self.directory = tempfile.TemporaryDirectory()
        self.previous_settings = getattr(flows, "settings", None)
        flows.settings = SimpleNamespace(pack_title_suffix="by test")
        self.previous = access.store
        access.store = AccessStore(Path(self.directory.name) / 'db', 1)
        access.store.change(1, 3, 'user')

    async def asyncTearDown(self):
        access.store.db.close()
        flows.settings = self.previous_settings
        access.store = self.previous
        self.directory.cleanup()

    async def test_grid_requires_confirmation_and_change_discards_selection(self):
        self.assertIsNone(await confirmed_grid(self.message, self.state))
        self.message.text = CHANGE
        await confirmed_grid(self.message, self.state)
        self.message.text = CONFIRM
        self.assertIsNone(await confirmed_grid(self.message, self.state))
        self.message.text = '7×5'
        await confirmed_grid(self.message, self.state)
        self.message.text = CONFIRM
        self.assertEqual(await confirmed_grid(self.message, self.state), (7, 5))

    async def test_both_grid_flows_wait_for_confirmation(self):
        await self.state.set_state(flows.RenderFlow.waiting_for_grid)
        await flows.receive_grid(self.message, self.state)
        self.assertEqual(await self.state.get_state(), flows.RenderFlow.waiting_for_grid.state)
        self.message.text = CONFIRM
        await flows.receive_grid(self.message, self.state)
        self.assertEqual(await self.state.get_state(), flows.RenderFlow.waiting_for_pack_title.state)
        self.message.text = '2x1'
        await self.state.set_state(flows.RenderFlow.waiting_for_existing_tg_art_grid)
        await self.state.update_data(existing_tg_art_stickers=[{'custom_emoji_id': '1', 'fallback_emoji': '▫️'}])
        await flows.receive_existing_tg_art_grid(self.message, self.state, AsyncMock())
        self.assertEqual(await self.state.get_state(), flows.RenderFlow.waiting_for_existing_tg_art_grid.state)
        self.message.text = CONFIRM
        await flows.receive_existing_tg_art_grid(self.message, self.state, AsyncMock())
        self.assertIsNone(await self.state.get_state())

    async def test_contact_without_id_does_not_grant_access(self):
        self.message.from_user.id = 1
        self.message.contact = SimpleNamespace(user_id=None)
        await self.state.update_data(admin_action='user')
        await admin.target(self.message, self.state)
        self.assertEqual(len(access.store.users()), 2)

    async def test_contact_id_grants_access(self):
        self.message.from_user.id = 1
        self.message.contact = SimpleNamespace(user_id=9)
        await self.state.update_data(admin_action='user')
        await admin.target(self.message, self.state)
        self.assertEqual(access.store.role(9), 'user')

    async def test_dispatcher_access_admin_cancel_and_download_routing(self):
        dispatcher = Dispatcher(storage=MemoryStorage())
        middleware = AccessMiddleware()
        dispatcher.message.outer_middleware(middleware)
        dispatcher.callback_query.outer_middleware(middleware)
        admin.reset_flow = flows._reset_flow
        admin.start_flow = flows.start
        dispatcher.include_router(admin.router)
        analysis_flow.reset_flow = flows._reset_flow
        analysis_flow.settings = SimpleNamespace(temp_dir=Path(self.directory.name))
        dispatcher.include_router(analysis_flow.router)
        dispatcher.include_router(flows.download_router)
        dispatcher.include_router(flows.router)
        telegram = Bot('123456:FAKE_TOKEN_FOR_TESTS')
        sequence = 0
        async def send(text, uid=3):
            nonlocal sequence
            sequence += 1
            message = {'message_id': sequence, 'date': 0, 'chat': {'id': uid, 'type': 'private'}, 'from': {'id': uid, 'is_bot': False, 'first_name': 'Test'}, 'text': text}
            if text.startswith('/'):
                message['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text)}]
            await dispatcher.feed_update(telegram, Update.model_validate({'update_id': 1, 'message': message}))
        with patch('aiogram.client.session.aiohttp.AiohttpSession.make_request', new_callable=AsyncMock) as request:
            await send('/start', 99)
            self.assertIn('Доступ закрыт', request.call_args.args[1].text)
            await send('/start')
            state = dispatcher.fsm.get_context(bot=telegram, chat_id=3, user_id=3)
            self.assertEqual(await state.get_state(), flows.RenderFlow.waiting_for_mode.state)
            await send('/users')
            self.assertIn('нет прав', request.call_args.args[1].text)
            await send('/users', 1)
            await send('➕ Добавить пользователя', 1)
            owner_state = dispatcher.fsm.get_context(bot=telegram, chat_id=1, user_id=1)
            self.assertEqual(await owner_state.get_state(), admin.AdminFlow.target.state)
            await send('/cancel', 1)
            self.assertIsNone(await owner_state.get_state())
            await state.set_state('DownloadFlow:collecting')
            await send('/cancel')
            self.assertIsNone(await state.get_state())
            await send('/analyze')
            self.assertEqual(await state.get_state(), analysis_flow.AnalysisFlow.collecting.state)
            async def delayed_request(*args, **kwargs):
                await asyncio.sleep(0.01)
            request.side_effect = delayed_request
            await asyncio.gather(send('Первая часть'), send('Вторая часть'))
            self.assertEqual(len((await state.get_data())['analysis_messages']), 2)
            request.side_effect = None
            await send('/done')
            self.assertIsNone(await state.get_state())
            self.assertEqual(request.call_args.args[1].document.filename, 'post-analysis.json')
            access.store.change(1, 3, None)
            await send('/start')
            self.assertIn('Доступ закрыт', request.call_args.args[1].text)
        await telegram.session.close()

    async def test_created_pack_preview_offers_grid_and_checks_owner(self):
        preview = {'user_id': 3, 'pack_name': 'art_by_testbot', 'columns': 2, 'rows': 1}
        flows.tg_art_previews['test-preview'] = preview
        callback = SimpleNamespace(from_user=SimpleNamespace(id=3), data='tg_art:test-preview', message=self.message, answer=AsyncMock())
        telegram = AsyncMock()
        telegram.get_sticker_set.return_value = SimpleNamespace(stickers=[SimpleNamespace(custom_emoji_id='1', emoji='🎨')])
        await flows.send_tg_art_preview(callback, telegram, self.state)
        self.assertEqual(await self.state.get_state(), flows.RenderFlow.waiting_for_existing_tg_art_grid.state)
        self.assertEqual((await self.state.get_data())['pending_grid'], [2, 1])
        access.store.change(1, 4, 'user')
        callback.from_user.id = 4
        telegram.get_sticker_set.reset_mock()
        await flows.send_tg_art_preview(callback, telegram, self.state)
        telegram.get_sticker_set.assert_not_called()
        flows.tg_art_previews.pop('test-preview')

    async def test_missing_set_and_taken_name(self):
        bot = AsyncMock()
        missing = TelegramBadRequest(method=GetStickerSet(name='art_by_testbot'), message='Bad Request: STICKERSET_INVALID')
        bot.get_sticker_set.side_effect = [SimpleNamespace(), missing]
        self.assertEqual(await available_pack_name(bot, 'art', 'testbot'), 'art_2_by_testbot')

    async def test_network_errors_are_not_treated_as_free_names(self):
        bot = AsyncMock()
        bot.get_sticker_set.side_effect = RuntimeError('network')
        with self.assertRaises(RuntimeError):
            await available_pack_name(bot, 'art', 'testbot')


class KeyboardAndNamesTest(unittest.TestCase):
    def test_coordinates_and_highlight(self):
        keyboard = grid_keyboard(7, 5).keyboard
        self.assertEqual(len(keyboard[:12]), 12)
        self.assertTrue(all(len(row) == 12 for row in keyboard[:12]))
        self.assertEqual(len({button.text for row in keyboard[:12] for button in row}), 144)
        self.assertEqual(sum(button.model_dump(exclude_none=True).get('style') == 'success' for row in keyboard[:12] for button in row), 35)
        self.assertEqual(parse_grid(keyboard[4][6].text), (7, 5))
        for invalid in ['0x2', '20x20', '13x16', 'x', '21x1']:
            with self.assertRaises(ValueError):
                parse_grid(invalid)

    def test_russian_and_nonletter_titles(self):
        self.assertEqual(suggest_pack_name('Летний арт'), 'letniy_art')
        for title in ['123', '🎨', '你好', 'Щука!']:
            result = make_sticker_set_name(suggest_pack_name(title), 'testbot')
            self.assertTrue(result[0].isalpha())
            self.assertLessEqual(len(result), 64)
            self.assertNotIn('__', result)
