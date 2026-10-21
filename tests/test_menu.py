from dataclasses import replace
import json

from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update

from bot import register_commands
from kernel.contact import contact_keyboard, menu_keyboard
from tests.helpers import ALICE, OWNER, message
from tests.test_contact import ContactFixture


class MenuTests(ContactFixture):
    async def test_handlers_send_reply_markup_in_actual_send_message_request(self):
        for user_id in (ALICE, OWNER):
            for i, command in enumerate(('/start', '/menu', '/cancel', '/contact', 'Написать'), 1):
                with self.subTest(user_id=user_id, command=command):
                    self.session.calls.clear()
                    with self.assertLogs('kernel.contact', level='INFO') as logs:
                        await self.feed(message(user_id, 200 + i, command))
                    # Inspect the Telegram method received by the session after
                    # real Dispatcher -> handler -> safe_notice -> send -> Bot.
                    self.assertEqual(len(self.session.calls), 1)
                    request = self.session.calls[0]
                    self.assertIsInstance(request, SendMessage)
                    self.assertEqual(request.chat_id, user_id)
                    self.assertIsInstance(request.reply_markup, ReplyKeyboardMarkup)
                    self.assertNotIsInstance(request.reply_markup, InlineKeyboardMarkup)
                    # Use aiogram's real serialization for the outgoing markup.
                    payload = json.loads(self.session.prepare_value(request.reply_markup, self.bot, {}))
                    self.assertIn('keyboard', payload)
                    self.assertNotIn('inline_keyboard', payload)
                    self.assertTrue(payload['resize_keyboard'])
                    self.assertTrue(payload['is_persistent'])
                    self.assertFalse(payload['one_time_keyboard'])
                    expected = ([['/cancel', '/menu']] if command in ('/contact', 'Написать')
                                else [['/home/tka'], ['Написать']])
                    self.assertEqual([[b['text'] for b in row] for row in payload['keyboard']], expected)
                    self.assertTrue(any('type=ReplyKeyboardMarkup' in line for line in logs.output))

    async def test_contact_navigation_cannot_send_inline_markup(self):
        invalid = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Написать', callback_data='menu:contact'),
        ]])
        with self.assertRaisesRegex(TypeError, 'ReplyKeyboardMarkup'):
            await self.service.safe_notice(ALICE, 'menu', reply_markup=invalid)
        self.assertFalse(self.session.calls)

    async def test_contact_keyboard_switches_back_on_all_menu_commands(self):
        for i, command in enumerate(('/start', '/menu', '/cancel')):
            with self.subTest(command=command):
                await self.feed(message(ALICE, i * 10 + 1, '/contact'))
                keyboard = self.session.calls[-1].reply_markup
                self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
                self.assertEqual([[b.text for b in row] for row in keyboard.keyboard], [['/cancel', '/menu']])
                self.assertTrue(keyboard.is_persistent)
                self.assertTrue(keyboard.resize_keyboard)
                self.assertFalse(keyboard.one_time_keyboard)
                self.assertTrue(self.store.contact_active(ALICE))
                await self.feed(message(ALICE, i * 10 + 2, command))
                keyboard = self.session.calls[-1].reply_markup
                self.assertEqual(keyboard, menu_keyboard(self.settings))
                self.assertTrue(keyboard.is_persistent)
                self.assertTrue(keyboard.resize_keyboard)
                self.assertFalse(keyboard.one_time_keyboard)
                self.assertFalse(self.store.contact_active(ALICE))
        self.assertIsNone(self.store.next_pending())

    async def test_write_button_switches_keyboard_and_is_not_forwarded(self):
        await self.feed(message(ALICE, 1, 'Написать'))
        self.assertEqual(self.session.calls[-1].reply_markup, contact_keyboard())
        self.assertTrue(self.store.contact_active(ALICE))
        self.assertIsNone(self.store.next_pending())
        await self.feed(message(ALICE, 2, 'real contact message'))
        self.assertEqual(self.store.next_pending()['content_text'], 'real contact message')

    async def test_home_placeholder_shows_menu_without_contact_delivery(self):
        await self.feed(message(ALICE, 1, '/contact'))
        await self.feed(message(ALICE, 2, '/home/tka'))
        call = self.session.calls[-1]
        self.assertIn('/home/tka готовится', call.text)
        self.assertEqual(call.reply_markup, menu_keyboard(self.settings))
        self.assertFalse(self.store.contact_active(ALICE))
        self.assertIsNone(self.store.next_pending())

    async def test_configured_web_app_uses_reply_button_and_exact_url(self):
        settings = replace(self.settings, mini_app_url='https://example.test/home/tka')
        keyboard = menu_keyboard(settings)
        self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
        self.assertEqual(keyboard.keyboard[0][0].web_app.url, settings.mini_app_url)
        self.assertTrue(keyboard.is_persistent)

    async def test_owner_contact_keyboard_preserves_reply_routing(self):
        await self.open_and_send()
        await self.drain()
        await self.feed(message(OWNER, 1, '/contact'))
        self.assertEqual(self.session.calls[-1].reply_markup, contact_keyboard())
        self.assertTrue(self.store.contact_active(OWNER))
        # This exact button label is ordinary answer content when sent by owner
        # with Reply. It must not be swallowed by the new text button handler.
        await self.feed(message(OWNER, 2, 'Написать', reply=self.note()))
        await self.drain()
        self.assertEqual((self.copies()[-1].chat_id, self.copies()[-1].message_id), (ALICE, 2))
        await self.feed(message(OWNER, 3, '/cancel'))
        self.assertEqual(self.session.calls[-1].reply_markup, menu_keyboard(self.settings))
        self.assertFalse(self.store.contact_active(OWNER))
        await self.feed(message(OWNER, 4, 'after cancel', reply=self.note()))
        await self.drain()
        self.assertEqual((self.copies()[-1].chat_id, self.copies()[-1].message_id), (ALICE, 4))

    async def test_main_menu_button_text_in_group_is_ignored(self):
        for i, text in enumerate(('Написать', '/home/tka'), 1):
            await self.feed(message(ALICE, i, text, chat_id=-10055, chat_type='supergroup'))
        self.assertFalse(self.session.calls)
        self.assertFalse(self.store.contact_active(ALICE))

    async def test_set_my_commands_registers_exact_four_commands(self):
        await register_commands(self.bot)
        call = self.session.calls[-1]
        self.assertEqual(call.__api_method__, 'setMyCommands')
        self.assertEqual([(c.command, c.description) for c in call.commands], [
            ('start', 'запуск'), ('menu', 'главное меню'),
            ('contact', 'написать владельцу'), ('cancel', 'выйти из режима переписки'),
        ])

    async def test_legacy_inline_menu_clicks_remove_old_markup(self):
        for i, action in enumerate(('menu:contact', 'menu:home'), 1):
            with self.subTest(action=action):
                await self.dp.feed_update(self.bot, Update.model_validate({
                    'update_id': 900 + i, 'callback_query': {
                        'id': f'legacy-{i}',
                        'from': {'id': ALICE, 'is_bot': False, 'first_name': 'Alice'},
                        'chat_instance': 'chat', 'data': action,
                        'message': message(ALICE, 100 + i).model_dump(mode='json', by_alias=True),
                    },
                }))
                edit = next(c for c in reversed(self.session.calls) if c.__api_method__ == 'editMessageReplyMarkup')
                self.assertIsNone(edit.reply_markup)
                expected = contact_keyboard() if action == 'menu:contact' else menu_keyboard(self.settings)
                self.assertEqual(self.session.calls[-1].reply_markup, expected)
                self.assertEqual(self.store.contact_active(ALICE), action == 'menu:contact')
