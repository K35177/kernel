from datetime import datetime, timezone

from aiogram.types import Update

from tests.helpers import ALICE, BOB, OWNER, message
from tests.test_contact import ContactFixture


class JoinRequestTests(ContactFixture):
    async def join(self, chat_id=None):
        data = {
            'update_id': 101,
            'chat_join_request': {
                'chat': {'id': chat_id or self.settings.root_group_id,
                         'type': 'supergroup', 'title': 'root', 'is_forum': True},
                'from': {'id': ALICE, 'is_bot': False, 'first_name': 'Alice'},
                'user_chat_id': ALICE,
                'date': datetime.now(timezone.utc).timestamp(),
            },
        }
        await self.dp.feed_update(self.bot, Update.model_validate(data))

    async def callback(self, actor, data):
        await self.dp.feed_update(self.bot, Update.model_validate({
            'update_id': 102,
            'callback_query': {
                'id': 'moderation', 'from': {'id': actor, 'is_bot': False, 'first_name': 'Actor'},
                'chat_instance': 'chat', 'data': data,
                'message': message(OWNER, 100, 'join request').model_dump(mode='json', by_alias=True),
            },
        }))

    async def test_join_request_keeps_existing_moderation_buttons(self):
        await self.join()
        call = self.session.calls[-1]
        self.assertEqual(call.chat_id, OWNER)
        self.assertIn(str(ALICE), call.text)
        self.assertEqual([b.callback_data for b in call.reply_markup.inline_keyboard[0]],
                         [f'approve:{ALICE}', f'decline:{ALICE}'])
        self.assertIsNone(self.store.next_pending())

    async def test_foreign_group_ignored(self):
        await self.join(-10099999)
        self.assertFalse(self.session.calls)

    async def test_owner_approves_and_declines_using_same_telegram_methods(self):
        await self.callback(OWNER, f'approve:{ALICE}')
        await self.callback(OWNER, f'decline:{BOB}')
        actions = [m for m in self.session.calls if m.__api_method__ in
                   ('approveChatJoinRequest', 'declineChatJoinRequest')]
        self.assertEqual([(m.__api_method__, m.chat_id, m.user_id) for m in actions], [
            ('approveChatJoinRequest', self.settings.root_group_id, ALICE),
            ('declineChatJoinRequest', self.settings.root_group_id, BOB),
        ])

    async def test_non_owner_cannot_moderate(self):
        await self.callback(BOB, f'approve:{ALICE}')
        self.assertEqual([m.__api_method__ for m in self.session.calls], ['answerCallbackQuery'])
        self.assertEqual(self.session.calls[-1].text, 'Permission denied')

    async def test_invalid_id_rejected(self):
        for data in ('approve:garbage', 'approve:-1', 'approve:0'):
            await self.callback(OWNER, data)
        self.assertFalse([m for m in self.session.calls if m.__api_method__ == 'approveChatJoinRequest'])
