import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiClient } from '../src/core/api.ts';
import { ApiError, NetworkError, NoTelegramContextError, SessionExpiredError } from '../src/core/types.ts';
import { pending, profile, response, status } from './helpers.ts';

const token = 'test_session_'.repeat(4);
test('one initData exchange serves concurrent me/status and uses bearer headers', async () => {
  const calls: { url: string; options: RequestInit }[] = [];
  const client = new ApiClient('/api', () => 'raw-proof', (async (url, options) => {
    calls.push({ url: String(url), options: options! });
    return response(String(url).endsWith('/auth/telegram') ? { access_token: token, token_type: 'bearer', expires_in: 900 }
      : String(url).endsWith('/me') ? profile : status());
  }) as typeof fetch);
  const result = await client.initialize();
  assert.equal(result.user.telegram_id, profile.telegram_id);
  assert.equal(calls.filter((c) => c.url.endsWith('/auth/telegram')).length, 1);
  assert.deepEqual(JSON.parse(calls[0].options.body as string), { initData: 'raw-proof' });
  for (const call of calls.slice(1)) {
    assert.equal((call.options.headers as Record<string, string>).Authorization, 'Bearer ' + token);
    assert.equal(call.options.credentials, 'omit');
  }
});
test('missing Telegram initData never calls the backend', async () => {
  let calls = 0;
  const client = new ApiClient('/api', () => '', (async () => { calls++; return response({}); }) as typeof fetch);
  await assert.rejects(() => client.status(), NoTelegramContextError);
  assert.equal(calls, 0);
});
test('expired memory session needs fresh initData and never replays consumed proof', async () => {
  let clock = 0; let raw = 'proof-a'; let exchanges = 0;
  const client = new ApiClient('/api', () => raw, (async (url) => {
    if (String(url).endsWith('/auth/telegram')) { exchanges++; return response({ access_token: token, token_type: 'bearer', expires_in: 60 }); }
    return response(status());
  }) as typeof fetch, () => clock);
  await client.status(); clock = 60001;
  await assert.rejects(() => client.status(), SessionExpiredError); assert.equal(exchanges, 1);
  raw = 'fresh-proof-b'; await client.status(); assert.equal(exchanges, 2);
});
test('server 401 invalidates session without looping or resubmitting old initData', async () => {
  let requests = 0; let unauthorized = false;
  const client = new ApiClient('/api', () => 'proof', (async (url) => {
    requests++;
    return String(url).endsWith('/auth/telegram') ? response({ access_token: token, token_type: 'bearer', expires_in: 900 })
      : response(status(), unauthorized ? 401 : 200);
  }) as typeof fetch);
  await client.status(); unauthorized = true;
  await assert.rejects(() => client.status(), SessionExpiredError); assert.equal(requests, 3);
});
test('backend replay response becomes a reopen instruction', async () => {
  const client = new ApiClient('/api', () => 'proof', (async () => response({}, 409)) as typeof fetch);
  await assert.rejects(() => client.status(), SessionExpiredError);
});
test('access request sends no arbitrary client user ID and returns idempotent result', async () => {
  const requests: RequestInit[] = [];
  const client = new ApiClient('/api', () => 'proof', (async (url, options) => {
    if (String(url).endsWith('/auth/telegram')) return response({ access_token: token, token_type: 'bearer', expires_in: 900 });
    requests.push(options!); return response({ ...pending(), created: requests.length === 1 });
  }) as typeof fetch);
  assert.equal((await client.requestAccess()).created, true);
  assert.equal((await client.requestAccess()).created, false);
  assert.equal(requests[0].method, 'POST'); assert.equal(requests[0].body, undefined);
});
test('network and backend errors never reflect the submitted secret data', async () => {
  const client = new ApiClient('/api', () => 'private-proof', (async () => { throw new Error('private-proof'); }) as typeof fetch);
  await assert.rejects(() => client.status(), (error: unknown) => error instanceof NetworkError && !error.message.includes('private-proof'));
  const rejected = new ApiClient('/api', () => 'private-proof', (async () => response({ detail: 'private-proof' }, 401)) as typeof fetch);
  await assert.rejects(() => rejected.status(), (error: unknown) => error instanceof ApiError && error.status === 401 && !error.message.includes('private-proof'));
});
test('unexpected JSON shape fails closed', async () => {
  const client = new ApiClient('/api', () => 'proof', (async (url) => response(String(url).endsWith('/auth/telegram')
    ? { access_token: token, token_type: 'bearer', expires_in: 900 } : { state: 'member' })) as typeof fetch);
  await assert.rejects(() => client.status(), ApiError);
});
