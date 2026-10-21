import unittest
from unittest.mock import patch

from backend.safety import RequestSafetyMiddleware
from kernel.backend_config import BackendSettings
from kernel.config import ConfigurationError
import os


class BackendConfigTests(unittest.TestCase):
    def test_default_is_local_with_cors_disabled(self):
        with patch.dict(os.environ,{},clear=True):
            options=BackendSettings.from_env()
        self.assertEqual(options.allowed_origins,())
        self.assertIn('127.0.0.1',options.allowed_hosts)

    def test_production_requires_explicit_https_origin(self):
        for extra in ({}, {'API_ALLOWED_ORIGINS':'*'}, {'API_ALLOWED_ORIGINS':'http://example.test'},
                      {'API_ALLOWED_ORIGINS':'https://example.test/private'}, {'API_ALLOWED_HOSTS':'*'}):
            with patch.dict(os.environ,{'API_ENV':'production',**extra},clear=True):
                with self.assertRaises(ConfigurationError):BackendSettings.from_env()
        with patch.dict(os.environ,{'API_ENV':'production','API_ALLOWED_ORIGINS':'https://example.test'},clear=True):
            self.assertEqual(BackendSettings.from_env().allowed_origins,('https://example.test',))

    def test_invalid_ttl_is_sanitized(self):
        with patch.dict(os.environ,{'AUTH_SESSION_TTL_SECONDS':'private-sentinel'},clear=True):
            with self.assertRaises(ConfigurationError) as error:BackendSettings.from_env()
        self.assertNotIn('private-sentinel',str(error.exception))


class ASGISafetyTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, query=b'', body=b'', headers=None, path='/api/auth/telegram', middleware=None):
        reached=[];sent=[]
        async def app(scope,receive,send):
            reached.append((await receive())['body'])
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'{}'})
        guard=middleware or RequestSafetyMiddleware(app,max_body=8,auth_per_minute=1)
        events=iter([{'type':'http.request','body':body,'more_body':False}])
        async def receive():return next(events)
        async def send(item):sent.append(item)
        await guard({'type':'http','path':path,'query_string':query,'headers':headers or [],'client':('local',1)},receive,send)
        return sent,reached,guard

    async def test_real_body_size_limit_even_without_content_length(self):
        sent,reached,_=await self.exercise(body=b'0424242001')
        self.assertEqual(sent[0]['status'],413);self.assertFalse(reached)

    async def test_query_auth_and_invalid_length_rejected(self):
        for kwargs in ({'query':b'token=private'},{'headers':[(b'content-length',b'invalid')]}):
            sent,reached,_=await self.exercise(**kwargs)
            self.assertEqual(sent[0]['status'],400);self.assertFalse(reached)
            self.assertNotIn(b'private',sent[1]['body'])

    async def test_valid_body_is_preserved_and_auth_is_rate_limited(self):
        sent,reached,guard=await self.exercise(body=b'test')
        self.assertEqual(sent[0]['status'],200);self.assertEqual(reached,[b'test'])
        self.assertIn((b'cache-control',b'no-store'),sent[0]['headers'])
        sent,_,_=await self.exercise(middleware=guard)
        self.assertEqual(sent[0]['status'],429)
