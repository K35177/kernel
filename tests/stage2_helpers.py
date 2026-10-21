import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
from datetime import datetime, timezone

from aiogram.types import ChatInviteLink, ChatMemberLeft, ChatMemberMember, User
from tests.helpers import ALICE, BOT_ID, TOKEN, FakeSession


def signed_init(user_id=ALICE, *, token=TOKEN, auth_date=None, extra=None, user_extra=None):
    user = {'id':user_id, 'first_name':'Alice', 'username':'alice_test', **(user_extra or {})}
    values = {'auth_date':str(int(time.time()) if auth_date is None else auth_date),
              'user':json.dumps(user, ensure_ascii=False, separators=(',',':')), **(extra or {})}
    check = '\n'.join(f'{key}={values[key]}' for key in sorted(values))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    values['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class AccessSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.members = {}
        self.invites = 0
        self.confirm_joins = True

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        if name not in ('getChatMember','createChatInviteLink','approveChatJoinRequest','declineChatJoinRequest','revokeChatInviteLink'):
            return await super().make_request(bot, method, timeout)
        self.calls.append(method)
        if self.errors.get(name):
            raise self.errors[name].pop(0)
        user = User(id=getattr(method, 'user_id', ALICE), first_name='Test', is_bot=False)
        if name == 'getChatMember':
            return (ChatMemberMember(status='member',user=user) if self.members.get(method.user_id, False)
                    else ChatMemberLeft(status='left',user=user))
        if name == 'createChatInviteLink':
            self.invites += 1
            return ChatInviteLink(invite_link=f'https://t.me/+test-invite-{self.invites}',
                creator=User(id=BOT_ID,is_bot=True,first_name='kernel'),
                creates_join_request=True,is_primary=False,is_revoked=False,
                expire_date=method.expire_date)
        if name == 'approveChatJoinRequest' and self.confirm_joins:
            self.members[method.user_id] = True
        if name == 'revokeChatInviteLink':
            return ChatInviteLink(invite_link=method.invite_link,
                creator=User(id=BOT_ID,is_bot=True,first_name='kernel'),
                creates_join_request=True,is_primary=False,is_revoked=True)
        return True
