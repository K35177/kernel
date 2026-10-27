import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiClient } from '../src/core/api.ts';
import { ApiError } from '../src/core/types.ts';
import { loginDiagnostic } from '../src/core/diagnostics.ts';

test('default browser fetch keeps its global receiver and reaches auth endpoint', async () => {
  const previous = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = (function (this: unknown) {
    if (this !== globalThis) throw new TypeError('synthetic browser receiver check');
    requests++;
    return Promise.resolve(new Response('{}', { status: 401 }));
  }) as typeof fetch;
  try {
    const client = new ApiClient('/api', () => 'synthetic-browser-proof');
    await assert.rejects(client.initialize(), (error: unknown) => error instanceof ApiError
      && error.status === 401 && error.endpoint === 'telegram_auth');
    assert.equal(requests, 1);
  } finally { globalThis.fetch = previous; }
});

test('readable auth HTTP errors never become transport errors', async () => {
  for (const status of [401, 422, 429, 500, 503]) {
    const client = new ApiClient('/api', () => 'synthetic-browser-proof',
      (async () => new Response('{}', { status })) as typeof fetch);
    await assert.rejects(client.initialize(), (error: unknown) => {
      assert.ok(error instanceof ApiError);
      const diagnostic = loginDiagnostic({sdkPresent:true,initDataPresent:true,telegramContextHint:true}, error);
      assert.equal(diagnostic.httpStatus,status);
      assert.equal(diagnostic.stage,'auth');
      assert.equal(diagnostic.endpoint,'telegram_auth');
      assert.notEqual(diagnostic.code,'transport_or_cors_error');
      return true;
    });
  }
});
