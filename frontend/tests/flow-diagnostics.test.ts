import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiClient } from '../src/core/api.ts';
import { loginDiagnostic } from '../src/core/diagnostics.ts';
import { response, profile, status } from './helpers.ts';
import { setImmediate as nextTurn } from 'node:timers/promises';

const launch = { sdkPresent: true, initDataPresent: true, telegramContextHint: true };
test('invalid auth response is attributed to auth rather than the protected API', async () => {
  const client = new ApiClient('/api', () => 'synthetic-proof', (async () => response({ unexpected: true })) as typeof fetch);
  try { await client.status(); assert.fail('invalid auth response accepted'); }
  catch (error) {
    assert.equal(loginDiagnostic(launch, error).stage, 'auth');
  }
});

const session = { access_token: 'synthetic_session_'.repeat(3), token_type: 'bearer', expires_in: 900 };
test('each failing protected endpoint is identified after a single successful auth exchange', async () => {
  for (const failing of ['/me', '/access/status']) {
    const paths: string[] = [];
    const client = new ApiClient('/api', () => 'synthetic-proof', (async (url) => {
      const path = String(url).slice(4); paths.push(path);
      if (path === '/auth/telegram') return response(session);
      if (path === failing) throw new Error('synthetic-private-transport-message');
      return response(path === '/me' ? profile : status());
    }) as typeof fetch);
    await assert.rejects(client.initialize(), (error: unknown) => {
      const report = loginDiagnostic(launch, error);
      assert.equal(report.code, 'transport_or_cors_error');
      assert.equal(report.endpoint, failing === '/me' ? 'me' : 'access_status');
      assert.equal(report.stage, 'api');
      assert.ok(!JSON.stringify(report).includes('synthetic-private'));
      return true;
    });
    assert.equal(paths.filter(path => path === '/auth/telegram').length, 1);
  }
});
test('wrong me schema is not attributed to the other parallel status request', async () => {
  const client = new ApiClient('/api', () => 'synthetic-proof', (async (url) => response(
    String(url).endsWith('/auth/telegram') ? session : String(url).endsWith('/me') ? {} : status())) as typeof fetch);
  await assert.rejects(client.initialize(), (error: unknown) => {
    const report = loginDiagnostic(launch, error);
    assert.equal(report.code, 'invalid_response'); assert.equal(report.endpoint, 'me');
    assert.equal(report.httpStatus, null); // No invented HTTP502 for a local schema check.
    return true;
  });
});
test('non-JSON auth response preserves the real HTTP status without echoing the body', async () => {
  const client = new ApiClient('/api', () => 'synthetic-proof', (async () => new Response('synthetic-private-non-json', { status: 200 })) as typeof fetch);
  await assert.rejects(client.status(), (error: unknown) => {
    const report = loginDiagnostic(launch, error);
    assert.equal(report.code, 'invalid_json'); assert.equal(report.stage, 'auth');
    assert.equal(report.endpoint, 'telegram_auth'); assert.equal(report.httpStatus, 200);
    assert.ok(!JSON.stringify(report).includes('synthetic-private'));
    return true;
  });
});
test('HTTP failures keep endpoint and actual HTTP status', async () => {
  const client = new ApiClient('/api', () => 'synthetic-proof', (async (url) => String(url).endsWith('/auth/telegram')
    ? response(session) : response({ detail: 'synthetic-private-response' }, 503)) as typeof fetch);
  await assert.rejects(client.status(), (error: unknown) => {
    const report = loginDiagnostic(launch, error);
    assert.equal(report.code, 'http_error'); assert.equal(report.httpStatus, 503);
    assert.equal(report.endpoint, 'access_status'); return true;
  });
});
test('auth timeout is identifiable without waiting or changing the production deadline', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const client = new ApiClient('/api', () => 'synthetic-proof', (async (_url, options) => new Promise<Response>((_resolve, reject) => {
    options?.signal?.addEventListener('abort', () => reject(new DOMException('synthetic-private', 'AbortError')), { once: true });
  })) as typeof fetch);
  const failure = assert.rejects(client.status(), (error: unknown) => {
    const report = loginDiagnostic(launch, error);
    assert.equal(report.code, 'request_timeout'); assert.equal(report.endpoint, 'telegram_auth');
    assert.equal(report.httpStatus, null); return true;
  });
  t.mock.timers.tick(15000);
  await failure;
});
test('abort while reading JSON is a timeout rather than a fabricated server failure', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const client = new ApiClient('/api', () => 'synthetic-proof', (async (_url, options) => Object.assign(response({}), {
    json: () => new Promise((_resolve, reject) => options?.signal?.addEventListener('abort',
      () => reject(new DOMException('synthetic-private', 'AbortError')), { once: true })),
  })) as typeof fetch);
  const failure = assert.rejects(client.status(), (error: unknown) => {
    const report = loginDiagnostic(launch, error);
    assert.equal(report.code, 'request_timeout'); assert.equal(report.stage, 'auth'); return true;
  });
  await nextTurn(); t.mock.timers.tick(15000); await failure;
});
