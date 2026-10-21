"""HTTP integration tests use real FastAPI when backend test deps are installed."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

try:
    import fastapi
    import httpx
    from backend.app import create_app
    HAS_HTTP = True
except ModuleNotFoundError as exc:
    if exc.name not in ('fastapi', 'httpx', 'starlette', 'anyio'):
        raise
    HAS_HTTP = False

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatMember
from kernel.backend_config import BackendSettings
from kernel.config import Settings
from kernel.storage import Store
from tests.helpers import ALICE, BOB, OWNER, TOKEN
from tests.stage2_helpers import AccessSession, signed_init


@unittest.skipUnless(HAS_HTTP, 'FastAPI/httpx not installed; HTTP integration not executed')
class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        path=Path(self.temp.name)/'api.sqlite3'
        self.settings=Settings(TOKEN, -1004242420001, OWNER,path)
        self.session=AccessSession();self.bot=Bot(TOKEN,session=self.session)
        self.store=Store(path)
        options=BackendSettings(membership_ttl=0,allowed_origins=('https://mini.example.test',))
        self.app=create_app(self.settings,options,self.bot,self.store)
        self.lifespan=self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        transport=httpx.ASGITransport(app=self.app,raise_app_exceptions=False)
        self.client=httpx.AsyncClient(transport=transport,base_url='http://localhost')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None,None,None)
        await self.bot.session.close()
        self.temp.cleanup()

    async def login(self,uid=ALICE):
        result=await self.client.post('/api/auth/telegram',json={'initData':signed_init(uid)})
        self.assertEqual(result.status_code,200,result.text)
        return {'Authorization':'Bearer '+result.json()['access_token']}

    async def test_auth_and_me_and_replay(self):
        raw=signed_init()
        result=await self.client.post('/api/auth/telegram',json={'initData':raw})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.headers['cache-control'],'no-store')
        headers={'Authorization':'Bearer '+result.json()['access_token']}
        self.assertEqual((await self.client.get('/api/me',headers=headers)).json()['telegram_id'],ALICE)
        self.assertEqual((await self.client.post('/api/auth/telegram',json={'initData':raw})).status_code,409)

    async def test_all_personal_and_admin_routes_require_auth(self):
        for method,url in [('get','/api/me'),('get','/api/access/status'),('post','/api/access/request'),
                           ('get','/api/admin/access/requests')]:
            result=await getattr(self.client,method)(url)
            self.assertEqual(result.status_code,401,(url,result.text))

    async def test_forged_and_expired_auth_rejected_without_echo(self):
        for raw in (signed_init(token='123:fake-key'),signed_init(auth_date=1)):
            response=await self.client.post('/api/auth/telegram',json={'initData':raw})
            self.assertEqual(response.status_code,401)
            self.assertNotIn(raw,response.text)
        response=await self.client.post('/api/auth/telegram',json={'initDataUnsafe':{'user':{'id':OWNER}},'user_id':OWNER})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('initDataUnsafe',response.text)

    async def test_repeated_request_and_owner_only_decision(self):
        user=await self.login();owner=await self.login(OWNER)
        first=await self.client.post('/api/access/request',headers=user)
        second=await self.client.post('/api/access/request',headers=user)
        self.assertTrue(first.json()['created']);self.assertFalse(second.json()['created'])
        request_id=first.json()['request_id']
        url=f'/api/admin/access/requests/{request_id}/decision'
        self.assertEqual((await self.client.post(url,headers=user,json={'decision':'approved'})).status_code,403)
        response=await self.client.post(url,headers=owner,json={'decision':'approved'})
        self.assertTrue(response.json()['changed'])
        self.assertFalse((await self.client.post(url,headers=owner,json={'decision':'rejected'})).json()['changed'])
        state=(await self.client.get('/api/access/status',headers=user)).json()
        self.assertEqual(state['state'],'awaiting_join');self.assertFalse(state['membership_confirmed'])

    async def test_existing_member_does_not_create_request(self):
        headers=await self.login();self.session.members[ALICE]=True
        response=await self.client.post('/api/access/request',headers=headers)
        self.assertFalse(response.json()['created'])
        self.assertEqual(response.json()['access_label'],'access: granted')

    async def test_telegram_failure_does_not_register_request(self):
        headers=await self.login()
        self.session.errors['getChatMember']=[TelegramNetworkError(method=GetChatMember(chat_id=-1,user_id=ALICE),message='timeout')]
        self.assertEqual((await self.client.post('/api/access/request',headers=headers)).status_code,503)
        self.assertIsNone(self.app.state.access.repository.latest(ALICE))

    async def test_cors_allows_only_configured_origin(self):
        for origin,allowed in [('https://mini.example.test',True),('https://attacker.example.test',False)]:
            response=await self.client.options('/api/me',headers={'Origin':origin,
                'Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'authorization'})
            self.assertEqual(response.headers.get('access-control-allow-origin'),origin if allowed else None)
        self.assertNotIn('access-control-allow-credentials',response.headers)

    async def test_validation_error_does_not_echo_secret_input(self):
        response=await self.client.post('/api/auth/telegram',json={'initData':'private-sentinel','user_id':OWNER})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('private-sentinel',response.text)
