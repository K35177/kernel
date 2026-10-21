import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import parse_qsl, urlencode

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import ApproveChatJoinRequest, CreateChatInviteLink, GetChatMember, SendMessage
from aiogram.types import ChatJoinRequest, Update

from bot import create_dispatcher
from kernel.access import AccessService, MembershipUnavailable
from kernel.access_store import AccessStore, ReplayError
from kernel.auth import AuthenticationError, validate_init_data
from kernel.backend_config import BackendSettings
from kernel.config import Settings
from kernel.storage import SCHEMA, Store
from tests.helpers import ALICE, BOB, OWNER, TOKEN, message
from tests.stage2_helpers import AccessSession, signed_init


class AuthenticationTests(unittest.TestCase):
    def test_valid_telegram_hmac_including_signature_field(self):
        raw = signed_init(extra={'query_id':'test-query', 'signature':'secondary-signature'})
        user = validate_init_data(raw, TOKEN)
        self.assertEqual(user.id, ALICE)
        self.assertEqual(user.display_name, 'Alice')

    def test_tampered_signature_and_forged_user_are_rejected(self):
        raw = signed_init()
        for forged in (raw.replace('Alice','Mallory'), signed_init(token='424242000:wrong-signing-key')):
            with self.assertRaises(AuthenticationError): validate_init_data(forged, TOKEN)

    def test_expired_and_future_auth_dates_rejected(self):
        now = int(time.time())
        for date in (now - 301, now + 31):
            with self.assertRaises(AuthenticationError):
                validate_init_data(signed_init(auth_date=date), TOKEN, now=now)

    def test_duplicate_keys_and_malformed_encoding_rejected(self):
        for raw in (signed_init() + '&auth_date=1', signed_init() + '&bad=%xy', 'user=%FF&hash=0', ''):
            with self.assertRaises(AuthenticationError): validate_init_data(raw, TOKEN)

    def test_signed_invalid_user_cannot_supply_identity(self):
        for extra in ({'id':True}, {'id':-1}, {'first_name':123}, {'is_bot':True}):
            with self.assertRaises(AuthenticationError): validate_init_data(signed_init(user_extra=extra), TOKEN)


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'kernel.sqlite3'
        self.store = Store(self.path); self.store.initialize()
        self.repo = AccessStore(self.store)
        self.session = AccessSession(); self.bot = Bot(TOKEN, session=self.session)
        self.settings = Settings(TOKEN, -1004242420001, OWNER,self.path)
        self.options = BackendSettings(membership_ttl=0,reapply_cooldown=0)
        self.service = AccessService(self.store,self.bot,self.settings,self.options)
        for uid in (ALICE, BOB, OWNER):
            self.store.set_contact(uid,'Test',None,False)

    async def asyncTearDown(self):
        await self.bot.session.close(); self.temp.cleanup()

    async def drain(self):
        for _ in range(20):
            if not await self.service.process_one(): return
        self.fail('Outbox did not drain')

    async def application(self):
        row,created = await self.service.request(ALICE)
        self.assertTrue(created)
        return row

    async def test_sessions_are_short_lived_hashed_and_replay_protected(self):
        identity = validate_init_data(signed_init(),TOKEN)
        token = self.repo.exchange(identity,identity.fingerprint,identity.proof_expires_at,60)
        self.assertEqual(self.repo.session_user(token)['telegram_id'],ALICE)
        with self.store.connection() as conn:
            stored = conn.execute('SELECT token_hash FROM auth_sessions').fetchone()[0]
            self.assertNotEqual(stored,token)
            conn.execute('UPDATE auth_sessions SET expires_at=0')
        self.assertIsNone(self.repo.session_user(token))
        with self.assertRaises(ReplayError):
            self.repo.exchange(identity,identity.fingerprint,identity.proof_expires_at,60)

    async def test_concurrent_requests_create_one_active_application(self):
        results = await asyncio.gather(*(self.service.request(ALICE) for _ in range(8)))
        self.assertEqual(sum(created for row,created in results),1)
        self.assertEqual(len({row['id'] for row,created in results}),1)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM access_outbox').fetchone()[0],1)

    async def test_existing_member_needs_no_application(self):
        self.session.members[ALICE] = True
        self.assertEqual(await self.service.request(ALICE),(None,False))
        self.assertIsNone(self.repo.latest(ALICE))
        self.assertEqual((await self.service.status(ALICE))['access_label'],'access: granted')

    async def test_membership_error_neither_creates_application_nor_grants_access(self):
        error=TelegramNetworkError(method=GetChatMember(chat_id=-1,user_id=ALICE),message='timeout')
        self.session.errors['getChatMember']=[error,error]
        with self.assertRaises(MembershipUnavailable): await self.service.request(ALICE)
        status=await self.service.status(ALICE)
        self.assertIsNone(status['membership_confirmed'])
        self.assertEqual(status['state'],'verification_unavailable')
        self.assertNotEqual(status['access_label'],'access: granted')
        self.assertIsNone(self.repo.latest(ALICE))

    async def test_initial_labels(self):
        status=await self.service.status(ALICE)
        self.assertEqual(status['access_label'],'access: not granted')
        self.assertEqual(status['request_label'],'request: not submitted')
        await self.application()
        self.assertEqual((await self.service.status(ALICE))['access_label'],'access: pending approval')

    async def test_concurrent_decisions_only_one_commits(self):
        row=await self.application()
        results=await asyncio.gather(self.service.decide(row['id'],'approved',OWNER),
                                     self.service.decide(row['id'],'rejected',OWNER))
        self.assertEqual(sum(changed for row,changed in results),1)
        self.assertEqual(len({row['status'] for row,changed in results}),1)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM access_outbox WHERE kind='user_decision'").fetchone()[0],1)

    async def test_unauthorized_owner_decision_rejected(self):
        row=await self.application()
        with self.assertRaises(PermissionError): await self.service.decide(row['id'],'approved',BOB)
        self.assertEqual(self.repo.get(row['id'])['status'],'pending')

    async def test_approval_is_not_membership_and_uses_join_request_link(self):
        row=await self.application()
        await self.service.decide(row['id'],'approved',OWNER)
        await self.drain()
        self.assertEqual(self.repo.get(row['id'])['status'],'approved')
        status=await self.service.status(ALICE)
        self.assertEqual(status['state'],'awaiting_join')
        self.assertFalse(status['membership_confirmed'])
        self.assertEqual(status['access_label'],'access: not granted')
        invite=next(m for m in self.session.calls if m.__api_method__=='createChatInviteLink')
        self.assertTrue(invite.creates_join_request)
        self.assertIsNone(invite.member_limit)
        self.assertIn('https://t.me/',status['invite_url'])

    async def test_join_after_approval_requires_matching_user_and_api_confirmation(self):
        row=await self.application(); await self.service.decide(row['id'],'approved',OWNER); await self.drain()
        invite=self.repo.invite(row['id'])
        def join(uid):
            from aiogram.types import ChatInviteLink, User
            return ChatJoinRequest(chat={'id':self.settings.root_group_id,'type':'supergroup'},
                from_user=User(id=uid,is_bot=False,first_name='Test'),user_chat_id=uid,date=int(time.time()),
                invite_link=ChatInviteLink(invite_link=invite['invite_link'],creator=User(id=self.bot.id,is_bot=True,first_name='kernel'),
                creates_join_request=True,is_primary=False,is_revoked=False))
        self.assertTrue(await self.service.handle_join(join(BOB)))
        self.assertEqual(self.session.calls[-1].__api_method__,'declineChatJoinRequest')
        self.assertEqual(self.repo.get(row['id'])['status'],'approved')
        self.assertTrue(await self.service.handle_join(join(ALICE)))
        await self.drain()
        self.assertEqual(self.repo.get(row['id'])['status'],'joined')
        self.assertTrue((await self.service.status(ALICE))['membership_confirmed'])
        self.assertIsNone(self.repo.invite(row['id']))

    async def test_successful_approve_api_without_membership_does_not_grant_access(self):
        row=await self.application(); await self.service.decide(row['id'],'approved',OWNER); await self.drain()
        self.session.confirm_joins=False
        self.repo.queue_join(row['id']); await self.service.process_one()
        self.assertEqual(self.repo.get(row['id'])['status'],'approved')
        self.assertFalse((await self.service.status(ALICE))['membership_confirmed'])
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT status FROM access_outbox WHERE kind='join_approval'").fetchone()[0],'pending')

    async def test_telegram_approve_error_remains_retryable(self):
        row=await self.application(); await self.service.decide(row['id'],'approved',OWNER); await self.drain()
        self.repo.queue_join(row['id'])
        self.session.errors['approveChatJoinRequest']=[TelegramBadRequest(
            method=ApproveChatJoinRequest(chat_id=-1,user_id=ALICE),message='not found')]
        await self.service.process_one()
        self.assertEqual(self.repo.get(row['id'])['status'],'approved')
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT status FROM access_outbox WHERE kind='join_approval'").fetchone()[0],'pending')

    async def test_blocked_owner_notification_persists_and_can_be_retried(self):
        row=await self.application()
        self.session.errors['sendMessage']=[TelegramForbiddenError(method=SendMessage(chat_id=OWNER,text='request'),message='blocked')]
        await self.service.process_one()
        self.assertEqual(self.repo.get(row['id'])['status'],'pending')
        self.repo.retry(row['id']); await self.service.process_one()
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM access_notifications').fetchone()[0],1)

    async def test_decline_and_repeat_button_preserve_first_decision(self):
        row=await self.application(); await self.service.decide(row['id'],'rejected',OWNER)
        repeated,changed=await self.service.decide(row['id'],'approved',OWNER)
        self.assertFalse(changed);self.assertEqual(repeated['status'],'rejected')
        await self.drain()
        self.assertFalse(any(m.__api_method__=='createChatInviteLink' for m in self.session.calls))
        self.assertEqual((await self.service.status(ALICE))['state'],'rejected')

    async def test_owner_callback_requires_saved_notification_and_is_idempotent(self):
        row=await self.application();await self.drain()
        dp,_=create_dispatcher(self.bot,self.settings,self.store)
        note=next(m for (chat,mid),m in self.session.sent.items() if chat==OWNER)
        async def click(actor,reply=note):
            update=Update.model_validate({'update_id':999,'callback_query':{
                'id':'access','from':{'id':actor,'is_bot':False,'first_name':'Actor'},'chat_instance':'chat',
                'data':'access:approve:'+row['id'],'message':reply.model_dump(mode='json',by_alias=True)}})
            await dp.feed_update(self.bot,update)
        await click(BOB);self.assertEqual(self.repo.get(row['id'])['status'],'pending')
        await click(OWNER,note.model_copy(update={'message_id':999999}))
        self.assertEqual(self.repo.get(row['id'])['status'],'pending')
        await click(OWNER);await click(OWNER)
        self.assertEqual(self.repo.get(row['id'])['status'],'approved')
        await dp.storage.close()

    async def test_session_auth_upsert_preserves_contact_mode(self):
        self.store.set_contact(ALICE,'Original',None,True)
        identity=validate_init_data(signed_init(),TOKEN)
        self.repo.exchange(identity,identity.fingerprint,identity.proof_expires_at,60)
        self.assertTrue(self.store.contact_active(ALICE))

    async def test_invite_api_error_keeps_decision_and_retryable_delivery(self):
        row=await self.application();await self.service.decide(row['id'],'approved',OWNER)
        self.session.errors['createChatInviteLink']=[TelegramNetworkError(
            method=CreateChatInviteLink(chat_id=-1),message='timeout')]
        await self.drain()
        self.assertEqual(self.repo.get(row['id'])['status'],'approved')
        self.assertIsNone(self.repo.invite(row['id']))
        deliveries=self.repo.list_requests()[0]['deliveries']
        self.assertTrue(any(d['kind']=='user_decision' and d['status']=='pending' for d in deliveries))
        self.assertFalse((await self.service.status(ALICE))['membership_confirmed'])

    async def test_worker_lease_survives_restart_and_cannot_be_completed_by_stale_claim(self):
        row=await self.application()
        first=self.repo.claim_job()
        reopened=AccessStore(Store(self.path))
        self.assertIsNone(reopened.claim_job())
        with self.store.connection() as conn:conn.execute('UPDATE access_outbox SET lease_until=0')
        second=reopened.claim_job()
        self.assertEqual(first['id'],second['id'])
        self.assertNotEqual(first['claim_token'],second['claim_token'])
        self.assertFalse(self.repo.finish_job(first))
        self.assertTrue(reopened.finish_job(second))

    async def test_v1_migration_preserves_history_and_reply_mapping(self):
        legacy=Store(Path(self.temp.name)/'legacy.sqlite3')
        with legacy.connection() as conn:
            conn.executescript(SCHEMA+'\nPRAGMA user_version=1;')
        legacy.set_contact(ALICE,'Alice',None,True)
        mid,_=legacy.enqueue(user_id=ALICE,full_name='Alice',username=None,direction='inbound',
            source_chat_id=ALICE,source_message_id=12,content_type='text',content_text='persistent history',payload_json='{}',sent_at=time.time())
        legacy.finish(mid,'delivered',mapping=(OWNER,4242,'notification'))
        legacy.initialize();legacy.initialize()
        self.assertEqual(legacy.resolve_reply(OWNER,4242)['user_id'],ALICE)
        self.assertEqual(legacy.get(mid)['content_text'],'persistent history')
        self.assertTrue(legacy.contact_active(ALICE))
        with legacy.connection() as conn:self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],2)
