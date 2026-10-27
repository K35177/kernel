"""Real FastAPI JSON -> real TypeScript client, synthetic DB and fake Telegram only."""
import json
import asyncio
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests import test_api as backend_tests
from tests.stage2_helpers import signed_init

ROOT = Path(__file__).resolve().parents[1]
CLIENT = (ROOT / 'frontend/src/core/api.ts').as_uri()
SCRIPT = r'''
import assert from 'node:assert/strict';
import { ApiClient } from CLIENT_URL;
let raw = ''; for await (const chunk of process.stdin) raw += chunk;
const fixture = JSON.parse(raw); const calls = [];
try {
  const client = new ApiClient('/api', () => fixture.proof, async (url, options) => {
    const path = String(url).slice(4); calls.push([path, options.method]);
    if (path === '/auth/telegram') {
      assert.deepEqual(JSON.parse(options.body), {initData:fixture.proof});
      assert.equal(options.headers['Content-Type'], 'application/json');
    } else {
      assert.equal(options.headers.Authorization, 'Bearer ' + fixture.replies['/auth/telegram'].body.access_token);
    }
    const reply = fixture.replies[path];
    return new Response(JSON.stringify(reply.body), {status:reply.status, headers:{'Content-Type':'application/json'}});
  });
  const result = await client.initialize();
  assert.equal(result.access.state, fixture.expected_state);
  assert.equal(typeof result.user.display_name, 'string');
  assert.deepEqual(calls, [['/auth/telegram','POST'],['/me','GET'],['/access/status','GET']]);
  process.stdout.write(JSON.stringify({contract_ok:true, request_count:3}));
} catch(error) {
  process.stderr.write(JSON.stringify({exception_class:error.constructor.name}));
  process.exitCode=1;
}
'''.replace('CLIENT_URL', json.dumps(CLIENT))


@unittest.skipUnless(backend_tests.HAS_HTTP and shutil.which('node'), 'FastAPI and Node required')
class FrontendAuthContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'synthetic.sqlite3'
        settings = backend_tests.Settings(backend_tests.TOKEN, -1004242420001, backend_tests.OWNER, path)
        self.session = backend_tests.AccessSession()
        bot = backend_tests.Bot(backend_tests.TOKEN, session=self.session)
        self.addAsyncCleanup(bot.session.close)
        app = backend_tests.create_app(settings, backend_tests.BackendSettings(membership_ttl=0), bot, backend_tests.Store(path))
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        self.client = backend_tests.httpx.AsyncClient(transport=backend_tests.httpx.ASGITransport(app=app), base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)

    async def contract(self, expected):
        proof = signed_init()
        auth = await self.client.post('/api/auth/telegram', json={'initData':proof})
        self.assertEqual(auth.status_code, 200)
        headers = {'Authorization':'Bearer ' + auth.json()['access_token']}
        replies = {'/auth/telegram':{'status':auth.status_code,'body':auth.json()}}
        for path in ('/me','/access/status'):
            response = await self.client.get('/api' + path, headers=headers)
            self.assertEqual(response.status_code, 200)
            replies[path] = {'status':response.status_code,'body':response.json()}
        completed = await asyncio.to_thread(subprocess.run, [shutil.which('node'), '--input-type=module', '--eval', SCRIPT],
            input=json.dumps({'proof':proof,'replies':replies,'expected_state':expected}),
            text=True, capture_output=True, timeout=20, cwd=ROOT)
        # Do not reflect response bodies, session tokens, proof or child stderr in failures.
        self.assertEqual(completed.returncode, 0, 'Synthetic TypeScript contract failed; raw output suppressed')
        self.assertEqual(json.loads(completed.stdout), {'contract_ok':True,'request_count':3})

    async def test_actual_auth_me_status_json_matches_frontend(self):
        await self.contract('not_submitted')

    async def test_membership_unavailable_is_valid_json_not_a_connection_failure(self):
        method = backend_tests.GetChatMember(chat_id=-1, user_id=backend_tests.ALICE)
        self.session.errors['getChatMember'] = [
            backend_tests.TelegramNetworkError(method=method, message='synthetic timeout') for _ in range(2)]
        await self.contract('verification_unavailable')
