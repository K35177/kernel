import asyncio
import logging
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import CopyMessage, SendMessage
from aiogram.types import Update

from bot import SecretRedactingFormatter, create_dispatcher
from kernel.config import Settings
from kernel.contact import clip, menu_keyboard
from kernel.storage import Store
from kernel.transport import ChatPacingMiddleware
from tests.helpers import ALICE, BOB, BOT_ID, OWNER, TOKEN, FakeSession, message


class ContactFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'kernel.sqlite3'
        self.store = Store(self.path)
        self.store.initialize()
        self.session = FakeSession()
        self.bot = Bot(TOKEN, session=self.session)
        self.settings = Settings(TOKEN, -1004242420001, OWNER, self.path)
        self.dp, self.service = create_dispatcher(self.bot, self.settings, self.store)
        self.update_id = 0

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()
        self.temp.cleanup()

    async def feed(self, msg):
        self.update_id += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=msg))

    async def open_and_send(self, user=ALICE, text='hello', message_id=2):
        await self.feed(message(user, 1, '/contact'))
        await self.feed(message(user, message_id, text))

    async def drain(self):
        for _ in range(100):
            row = self.store.next_pending()
            if not row:
                return
            await self.service.process(row)
        self.fail('Queue did not drain')

    def copies(self):
        return [call for call in self.session.calls if call.__api_method__ == 'copyMessage']

    def note(self, user=ALICE):
        for (chat_id, _), msg in self.session.sent.items():
            if chat_id == OWNER and f'Telegram ID: {user}\n' in msg.text:
                return msg
        self.fail('No contact notification')


class ContactTests(ContactFixture):
    async def test_start_menu_cancel_use_same_two_button_menu(self):
        menus = []
        for i, command in enumerate(('/start', '/menu', '/cancel'), 1):
            await self.feed(message(ALICE, i, command))
            menus.append(self.session.calls[-1].reply_markup)
        self.assertEqual(menus[0], menus[1])
        self.assertEqual(menus[1], menus[2])
        self.assertEqual([[b.text for b in row] for row in menus[0].keyboard],
                         [['/home/tka'], ['Написать']])
        self.assertFalse(self.store.contact_active(ALICE))
        self.assertEqual(menu_keyboard(self.settings), menus[0])

    async def test_contact_button_activates_persistent_session(self):
        await self.feed(message(ALICE, 1, '/start'))
        await self.feed(message(ALICE, 2, 'Написать'))
        self.assertTrue(Store(self.path).contact_active(ALICE))

    async def test_contact_home_button_does_not_invent_url(self):
        keyboard = menu_keyboard(self.settings)
        self.assertEqual(keyboard.keyboard[0][0].text, '/home/tka')
        self.assertIsNone(keyboard.keyboard[0][0].web_app)

    async def test_unlimited_messages_and_cancel(self):
        await self.open_and_send()
        for mid in range(3, 103):
            await self.feed(message(ALICE, mid, f'message {mid}'))
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM contact_messages').fetchone()[0], 101)
        await self.feed(message(ALICE, 103, '/cancel'))
        await self.feed(message(ALICE, 104, 'do not forward'))
        self.assertFalse(self.store.contact_active(ALICE))
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM contact_messages').fetchone()[0], 101)

    async def test_private_only_and_unknown_reply_fail_closed(self):
        await self.feed(message(ALICE, 1, '/contact', chat_id=-10055, chat_type='supergroup'))
        self.assertFalse(self.store.contact_active(ALICE))
        await self.feed(message(OWNER, 1, 'missing reply'))
        await self.feed(message(OWNER, 2, 'unknown', reply=message(BOT_ID, 999, 'Telegram ID: 424242002', chat_id=OWNER)))
        self.assertFalse(self.copies())
        self.assertIsNone(self.store.next_pending())
        self.assertIn('нет сохранённого адресата', self.session.calls[-1].text)

    async def test_replies_go_to_correct_user_for_interleaved_conversations(self):
        await self.open_and_send(ALICE, 'Alice first')
        await self.open_and_send(BOB, 'Bob first')
        await self.drain()
        await self.feed(message(OWNER, 10, 'answer Bob', reply=self.note(BOB)))
        await self.feed(message(OWNER, 11, 'answer Alice', reply=self.note(ALICE)))
        await self.drain()
        replies = [call for call in self.copies() if call.from_chat_id == OWNER]
        self.assertEqual([(c.chat_id, c.message_id) for c in replies], [(BOB, 10), (ALICE, 11)])

    async def test_copy_also_has_reply_mapping_and_survives_restart(self):
        await self.open_and_send()
        await self.drain()
        copy_id = next(call for call in reversed(self.session.calls) if call.__api_method__ == 'copyMessage')
        copied = self.session.sent[(OWNER, self.session.next_id)]
        self.assertEqual(copy_id.chat_id, OWNER)
        store2 = Store(self.path)
        store2.initialize()
        dp2, service2 = create_dispatcher(self.bot, self.settings, store2)
        self.assertTrue(store2.contact_active(ALICE))
        await dp2.feed_update(self.bot, Update(update_id=900, message=message(OWNER, 20, 'after restart', reply=copied)))
        await service2.process(store2.next_pending())
        self.assertEqual(self.copies()[-1].chat_id, ALICE)
        self.assertEqual(self.copies()[-1].message_id, 20)
        await dp2.storage.close()

    async def test_attacker_cannot_reply_as_owner_or_use_foreign_chat(self):
        await self.open_and_send()
        await self.drain()
        note = self.note()
        initial = len(self.copies())
        await self.feed(message(BOB, 30, 'attack', reply=note))
        await self.feed(message(OWNER, 31, 'attack', reply=note, chat_id=-10055, chat_type='supergroup'))
        await self.drain()
        self.assertEqual(len(self.copies()), initial)

    async def test_non_bot_reply_target_is_rejected_even_with_known_message_id(self):
        await self.open_and_send()
        await self.drain()
        target = self.note().model_copy(update={'from_user': message(BOB, 1).from_user})
        await self.feed(message(OWNER, 15, 'do not send', reply=target))
        self.assertIsNone(self.store.next_pending())

    async def test_history_includes_owner_answer_without_cross_user_leak(self):
        await self.open_and_send(ALICE, 'Alice secret')
        await self.open_and_send(BOB, 'Bob private')
        await self.drain()
        await self.feed(message(OWNER, 10, 'Owner answer', reply=self.note(ALICE)))
        await self.drain()
        await self.feed(message(ALICE, 3, 'Next Alice message'))
        row = self.store.next_pending()
        text = await self.service.notification(row)
        self.assertIn('Alice secret', text)
        self.assertIn('Owner answer', text)
        self.assertIn('владелец', text)
        self.assertIn('Next Alice message', text)
        self.assertNotIn('Bob private', text)
        self.assertIn('MSK', text)

    async def test_text_and_supported_attachments_copied_both_directions(self):
        await self.feed(message(ALICE, 1, '/contact'))
        samples = [
            {'photo': [{'file_id': 'photo', 'file_unique_id': 'p', 'width': 2, 'height': 2}], 'caption': 'photo caption'},
            {'document': {'file_id': 'doc', 'file_unique_id': 'd', 'file_name': 'report.txt'}},
            {'voice': {'file_id': 'voice', 'file_unique_id': 'v', 'duration': 1}},
            {'video': {'file_id': 'video', 'file_unique_id': 'vi', 'width': 2, 'height': 2, 'duration': 1}},
            {'audio': {'file_id': 'audio', 'file_unique_id': 'a', 'duration': 1}},
            {'sticker': {'file_id': 'sticker', 'file_unique_id': 's', 'type': 'regular',
                         'width': 2, 'height': 2, 'is_animated': False, 'is_video': False}},
            {'animation': {'file_id': 'anim', 'file_unique_id': 'an', 'width': 2, 'height': 2, 'duration': 1}},
            {'video_note': {'file_id': 'note', 'file_unique_id': 'vn', 'length': 2, 'duration': 1}},
            {'contact': {'phone_number': '+100000000', 'first_name': 'Test'}},
            {'location': {'latitude': 1.0, 'longitude': 2.0}},
            {'venue': {'location': {'latitude': 1.0, 'longitude': 2.0}, 'title': 'Venue', 'address': 'Address'}},
            {'dice': {'emoji': '🎲', 'value': 3}},
            {'poll': {'id': 'poll', 'question': 'Question?',
                      'options': [{'text': 'Yes', 'voter_count': 0, 'persistent_id': 'yes'},
                                  {'text': 'No', 'voter_count': 0, 'persistent_id': 'no'}],
                      'total_voter_count': 0, 'is_closed': False, 'is_anonymous': True,
                      'type': 'regular', 'allows_multiple_answers': False,
                      'allows_revoting': True, 'members_only': False}},
        ]
        for i, content in enumerate(samples, 2):
            with self.subTest(content=next(iter(content))):
                await self.feed(message(ALICE, i, None, **content))
                await self.drain()
                await self.feed(message(OWNER, i, None, reply=self.note(), **content))
                await self.drain()
        self.assertEqual(len(self.copies()), len(samples) * 2)
        self.assertEqual({c.chat_id for c in self.copies()}, {ALICE, OWNER})
        with self.store.connection() as conn:
            payloads = conn.execute('SELECT payload_json FROM contact_messages').fetchall()
        self.assertTrue(any('file_id' in row[0] for row in payloads))

    async def test_duplicate_updates_are_idempotent(self):
        await self.open_and_send()
        await self.feed(message(ALICE, 2, 'hello'))
        await self.drain()
        self.assertEqual(len(self.copies()), 1)
        note = self.note()
        await self.feed(message(OWNER, 10, 'answer', reply=note))
        await self.feed(message(OWNER, 10, 'answer', reply=note))
        await self.drain()
        self.assertEqual(len(self.copies()), 2)

    async def test_blocked_user_marks_failed_and_notifies_owner(self):
        await self.open_and_send()
        await self.drain()
        await self.feed(message(OWNER, 10, 'answer', reply=self.note()))
        row = self.store.next_pending()
        self.session.errors['copyMessage'] = [TelegramForbiddenError(
            method=CopyMessage(chat_id=ALICE, from_chat_id=OWNER, message_id=10), message='Forbidden')]
        await self.service.process(row)
        self.assertEqual(self.store.get(row['id'])['status'], 'failed')
        self.assertIn('не доставлено', self.session.calls[-1].text)
        self.assertIsNone(self.store.next_pending())

    async def test_429_is_persisted_and_resumed_once(self):
        await self.open_and_send()
        row = self.store.next_pending()
        self.session.errors['sendMessage'] = [TelegramRetryAfter(
            method=SendMessage(chat_id=OWNER, text='note'), message='Flood', retry_after=1)]
        await self.service.process(row)
        saved = self.store.get(row['id'])
        self.assertEqual(saved['status'], 'queued')
        self.assertGreater(saved['retry_at'], time.time())
        self.assertIsNone(self.store.next_pending())
        # Simulate the persisted deadline elapsing without sleeping.
        with self.store.connection() as conn:
            conn.execute('UPDATE contact_messages SET retry_at=0 WHERE id=?', (row['id'],))
        await self.drain()
        self.assertEqual(self.store.get(row['id'])['status'], 'delivered')
        self.assertEqual(len(self.copies()), 1)

    async def test_network_error_marks_uncertain_without_repeated_send(self):
        await self.open_and_send()
        await self.drain()
        await self.feed(message(OWNER, 10, 'answer', reply=self.note()))
        row = self.store.next_pending()
        self.session.errors['copyMessage'] = [TelegramNetworkError(
            method=CopyMessage(chat_id=ALICE, from_chat_id=OWNER, message_id=10), message='Timeout')]
        await self.service.process(row)
        self.assertEqual(self.store.get(row['id'])['status'], 'uncertain')
        self.assertIsNone(self.store.next_pending())
        self.assertIn('могло дойти', self.session.calls[-1].text)

    async def test_uncopyable_message_fails_without_crashing_queue(self):
        await self.open_and_send()
        row = self.store.next_pending()
        await self.service.process(row)
        self.session.errors['copyMessage'] = [TelegramBadRequest(
            method=CopyMessage(chat_id=OWNER, from_chat_id=ALICE, message_id=2), message='Cannot copy')]
        await self.drain()
        self.assertEqual(self.store.get(row['id'])['status'], 'failed')
        self.assertIn('не удалось', self.session.calls[-1].text)

    async def test_restart_retains_pending_but_quarantines_in_flight(self):
        await self.open_and_send()
        row = self.store.next_pending()
        store2 = Store(self.path)
        self.assertEqual(store2.next_pending()['id'], row['id'])
        self.assertTrue(store2.claim(row['id'], 'queued', 'notifying'))
        self.assertFalse(store2.claim(row['id'], 'queued', 'notifying'))
        self.assertEqual(store2.recover_interrupted(), 1)
        self.assertEqual(store2.get(row['id'])['status'], 'uncertain')
        self.assertEqual(store2.recover_interrupted(), 0)
        self.assertIsNone(store2.next_pending())

    async def test_sql_injection_and_html_stay_plain_text(self):
        text = "<b>hello</b> <script>alert(1)</script> '; DROP TABLE users; --"
        await self.open_and_send(text=text)
        await self.drain()
        self.assertIn(text, self.note().text)
        note_call = next(c for c in self.session.calls if c.__api_method__ == 'sendMessage' and 'kernel / contact' in c.text)
        self.assertIsNone(note_call.parse_mode)
        self.assertTrue(self.store.contact_active(ALICE))

    async def test_large_emoji_notification_fits_telegram_limit(self):
        await self.open_and_send(text='😄' * 4096)
        text = await self.service.notification(self.store.next_pending())
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
        self.assertIn('Ответ: Reply', text)
        self.assertEqual(clip('😄' * 5000, 20).encode('utf-16-le').decode('utf-16-le')[-1], '…')

    async def test_reply_to_moderation_or_menu_message_is_not_routed(self):
        await self.feed(message(OWNER, 1, '/start'))
        menu = self.session.sent[(OWNER, self.session.next_id)]
        await self.feed(message(OWNER, 2, 'no target', reply=menu))
        self.assertIsNone(self.store.next_pending())
        self.assertIn('нет сохранённого адресата', self.session.calls[-1].text)

    async def test_conflicting_mapping_cannot_change_recipient(self):
        await self.open_and_send(ALICE)
        await self.open_and_send(BOB)
        row1 = self.store.next_pending()
        self.store.finish(row1['id'], 'notified', mapping=(OWNER, 9999, 'notification'))
        with self.store.connection() as conn:
            row2 = conn.execute('SELECT id FROM contact_messages WHERE user_id=?', (BOB,)).fetchone()[0]
        with self.assertRaises(Exception):
            self.store.finish(row2, 'notified', mapping=(OWNER, 9999, 'notification'))
        self.assertEqual(self.store.resolve_reply(OWNER, 9999)['user_id'], ALICE)
        self.assertIsNone(self.store.resolve_reply(BOB, 9999))

    async def test_source_message_cannot_be_reassigned_to_other_user(self):
        await self.open_and_send()
        with self.assertRaises(ValueError):
            self.store.enqueue(user_id=BOB, full_name='Bob', username=None, direction='outbound',
                source_chat_id=ALICE, source_message_id=2, content_type='text',
                content_text='attack', payload_json='{}', sent_at=time.time())
        self.assertEqual(self.store.next_pending()['user_id'], ALICE)

    async def test_concurrent_contact_and_message_are_serialized(self):
        await asyncio.gather(
            self.feed(message(ALICE, 1, '/contact')),
            self.feed(message(ALICE, 2, 'immediate message')),
        )
        self.assertEqual(self.store.next_pending()['content_text'], 'immediate message')

    async def test_copy_retry_resumes_without_duplicate_notification(self):
        await self.open_and_send()
        await self.service.process(self.store.next_pending())
        row = self.store.next_pending()
        self.assertEqual(row['status'], 'notified')
        self.session.errors['copyMessage'] = [TelegramRetryAfter(
            method=CopyMessage(chat_id=OWNER, from_chat_id=ALICE, message_id=2), message='Flood', retry_after=1)]
        await self.service.process(row)
        self.assertEqual(self.store.get(row['id'])['status'], 'notified')
        with self.store.connection() as conn:
            conn.execute('UPDATE contact_messages SET retry_at=0 WHERE id=?', (row['id'],))
        store2 = Store(self.path)
        from kernel.contact import ContactService
        restarted = ContactService(store2, self.bot, self.settings)
        await restarted.process(store2.next_pending())
        notes = [m for m in self.session.calls if m.__api_method__ == 'sendMessage' and 'kernel / contact' in m.text]
        self.assertEqual(len(notes), 1)
        self.assertEqual(store2.get(row['id'])['status'], 'delivered')

    async def test_queued_reply_retains_destination_across_restart(self):
        await self.open_and_send(ALICE)
        await self.open_and_send(BOB)
        await self.drain()
        await self.feed(message(OWNER, 10, 'reply before restart', reply=self.note(ALICE)))
        store2 = Store(self.path)
        from kernel.contact import ContactService
        restarted = ContactService(store2, self.bot, self.settings)
        await restarted.process(store2.next_pending())
        self.assertEqual((self.copies()[-1].chat_id, self.copies()[-1].message_id), (ALICE, 10))

    async def test_worker_delivers_and_stops_cleanly(self):
        worker = asyncio.create_task(self.service.run())
        try:
            await self.open_and_send()
            async def delivered():
                while True:
                    with self.store.connection() as conn:
                        row = conn.execute('SELECT status FROM contact_messages').fetchone()
                    if row and row['status'] == 'delivered':
                        return
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(delivered(), timeout=3)
            self.assertEqual(len(self.copies()), 1)
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_database_error_after_send_does_not_repeat_ambiguous_write(self):
        await self.open_and_send()
        worker = asyncio.create_task(self.service.run())
        original = self.store.finish
        def fail_finish(*args, **kwargs):
            if kwargs.get('mapping'):
                raise sqlite3.OperationalError('simulated disk error after Telegram send')
            return original(*args, **kwargs)
        try:
            with patch.object(self.store, 'finish', side_effect=fail_finish):
                async def uncertain():
                    while True:
                        with self.store.connection() as conn:
                            row = conn.execute('SELECT status FROM contact_messages').fetchone()
                        if row and row['status'] == 'uncertain':
                            return
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(uncertain(), timeout=3)
            self.assertFalse(worker.done())
            self.assertIsNone(self.store.next_pending())
            note = self.note()
            self.assertIsNone(self.store.resolve_reply(OWNER, note.message_id))
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_chat_pacing_applies_to_copy_without_blocking_callbacks(self):
        middleware = ChatPacingMiddleware(chat_interval=0.02, global_interval=0.005)
        sent_at = []
        async def request(bot, method):
            sent_at.append(asyncio.get_running_loop().time())
            return True
        await asyncio.gather(*[
            middleware(request, self.bot, CopyMessage(chat_id=OWNER, from_chat_id=ALICE, message_id=i))
            for i in range(3)
        ])
        self.assertEqual(len(sent_at), 3)
        self.assertGreaterEqual(sent_at[1] - sent_at[0], 0.018)
        self.assertGreaterEqual(sent_at[2] - sent_at[1], 0.018)


class LoggingTests(unittest.TestCase):
    def test_token_redacted_in_message_and_traceback(self):
        formatter = SecretRedactingFormatter(TOKEN)
        try:
            raise RuntimeError(f'https://api.telegram.org/bot{TOKEN}/getMe')
        except RuntimeError:
            import sys
            record = logging.LogRecord('test', logging.ERROR, __file__, 1,
                'failure for %s', (TOKEN,), sys.exc_info())
        output = formatter.format(record)
        self.assertNotIn(TOKEN, output)
        self.assertIn('[REDACTED]', output)
