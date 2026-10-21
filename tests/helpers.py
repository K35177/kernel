from datetime import datetime, timezone

from aiogram.client.session.base import BaseSession
from aiogram.types import Message, MessageId, User

BOT_ID = 424242000
OWNER = 424242001
ALICE = 424242002
BOB = 424242003
TOKEN = f'{BOT_ID}:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'


class FakeSession(BaseSession):
    """Only Telegram API responses are faked; real dispatcher/filter/model code runs."""
    def __init__(self):
        super().__init__()
        self.calls = []
        self.sent = {}
        self.errors = {}
        self.next_id = 10000

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        self.calls.append(method)
        if self.errors.get(name):
            raise self.errors[name].pop(0)
        if name == 'getMe':
            return User(id=BOT_ID, is_bot=True, first_name='kernel', username='test_kernel_bot')
        if name == 'sendMessage':
            self.next_id += 1
            value = Message.model_validate({
                'message_id': self.next_id, 'date': datetime.now(timezone.utc),
                'chat': {'id': method.chat_id, 'type': 'private'},
                'from': {'id': BOT_ID, 'is_bot': True, 'first_name': 'kernel'},
                'text': method.text,
            }, context={'bot': bot})
            self.sent[(method.chat_id, self.next_id)] = value
            return value
        if name == 'copyMessage':
            self.next_id += 1
            self.sent[(method.chat_id, self.next_id)] = Message.model_validate({
                'message_id': self.next_id, 'date': datetime.now(timezone.utc),
                'chat': {'id': method.chat_id, 'type': 'private'},
                'from': {'id': BOT_ID, 'is_bot': True, 'first_name': 'kernel'},
                'text': '[copied message]',
            }, context={'bot': bot})
            return MessageId(message_id=self.next_id)
        return True


def message(user_id: int, message_id: int, text: str | None = 'hello', *, reply=None,
            chat_id=None, chat_type='private', **content):
    data = {
        'message_id': message_id, 'date': datetime.now(timezone.utc),
        'chat': {'id': chat_id if chat_id is not None else user_id, 'type': chat_type},
        'from': {'id': user_id, 'is_bot': user_id == BOT_ID, 'first_name': f'user-{user_id}',
                 'username': f'user_{user_id}'},
        **content,
    }
    if text is not None:
        data['text'] = text
    if reply is not None:
        data['reply_to_message'] = reply.model_dump(mode='json', by_alias=True, exclude_none=True)
    return Message.model_validate(data)
