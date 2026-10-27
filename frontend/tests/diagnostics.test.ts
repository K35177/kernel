import assert from 'node:assert/strict';
import test from 'node:test';
import { loginDiagnostic } from '../src/core/diagnostics.ts';
import { createTelegramAdapter } from '../src/core/telegram.ts';
import { ApiClient } from '../src/core/api.ts';
import { AppController } from '../src/core/controller.ts';
import { ApiError, NetworkError, SessionExpiredError } from '../src/core/types.ts';
import { lifecycle, profile, response, status } from './helpers.ts';

const secret = 'SYNTHETIC_PRIVATE_PROOF';
function telegram(raw = secret, platform = 'android') {
  return createTelegramAdapter(() => ({ initData: raw, platform, ready() {}, expand() {},
    get initDataUnsafe(): never { throw new Error('must not access identity'); } }));
}
test('SDK absence, browser, keyboard and signed context have separate presence reports', () => {
  const noSDK = createTelegramAdapter(() => undefined);
  assert.equal(loginDiagnostic(noSDK.inspectLaunch()).code, 'sdk_unavailable');
  assert.deepEqual(telegram('', 'unknown').inspectLaunch(), { sdkPresent: true, initDataPresent: false, telegramContextHint: false });
  assert.deepEqual(telegram('').inspectLaunch(), { sdkPresent: true, initDataPresent: false, telegramContextHint: true });
  assert.equal(loginDiagnostic(telegram().inspectLaunch()).code, 'init_data_present');
});
test('repeated keyboard refresh makes no auth or personal requests', async () => {
  let requests = 0;
  const client = new ApiClient('/api', () => '', (async () => { requests++; throw new Error(secret); }) as typeof fetch);
  const controller = new AppController(client, telegram(''), lifecycle);
  for (let n = 0; n < 3; n++) await controller.refresh();
  assert.equal(requests, 0);
  assert.equal(controller.getSnapshot().phase, 'needs-launch');
  assert.equal(controller.getSnapshot().diagnostic?.code, 'init_data_missing');
});
test('malformed proof rejected by server is auth_rejected, never a missing context', async () => {
  const rejected = telegram('synthetic-malformed-proof');
  const calls: string[] = [];
  const client = new ApiClient('/api', rejected.getInitData, (async (url) => {
    calls.push(String(url)); return response({ detail: secret }, 401);
  }) as typeof fetch);
  const controller = new AppController(client, rejected, lifecycle);
  await controller.refresh();
  const diagnostic = controller.getSnapshot().diagnostic!;
  assert.equal(controller.getSnapshot().phase, 'unauthorized');
  assert.deepEqual({ code: diagnostic.code, stage: diagnostic.stage, httpStatus: diagnostic.httpStatus },
    { code: 'auth_rejected', stage: 'auth', httpStatus: 401 });
  assert.deepEqual(calls, ['/api/auth/telegram']);
  assert.ok(!JSON.stringify(diagnostic).includes(secret));
});
test('auth payload rejection, HTTP failure and opaque network/CORS failures stay distinct', () => {
  const launch = telegram().inspectLaunch();
  for (const [error, code, stage, httpStatus] of [
    [new ApiError(422, 'auth'), 'auth_payload_rejected', 'auth', 422],
    [new ApiError(503), 'http_error', 'api', 503],
    [new ApiError(502, 'auth'), 'http_error', 'auth', 502],
    [new NetworkError('auth'), 'transport_or_cors_error', 'auth', null],
    [new NetworkError(), 'transport_or_cors_error', 'api', null],
    [new SessionExpiredError(), 'session_expired', 'auth', null],
  ] as const) {
    const diagnostic = loginDiagnostic(launch, error);
    assert.deepEqual([diagnostic.code, diagnostic.stage, diagnostic.httpStatus], [code, stage, httpStatus]);
  }
});
test('network rejection during auth never includes raw fetch error or proof', async () => {
  const context = telegram();
  const client = new ApiClient('/api', context.getInitData, (async () => { throw new Error(secret); }) as typeof fetch);
  const controller = new AppController(client, context, lifecycle);
  await controller.refresh();
  assert.equal(controller.getSnapshot().diagnostic?.code, 'transport_or_cors_error');
  assert.equal(controller.getSnapshot().diagnostic?.stage, 'auth');
  assert.ok(!JSON.stringify(controller.getSnapshot()).includes(secret));
});
test('diagnostics allowlist excludes arbitrary identity, SDK fields and exception messages', () => {
  const input = { ...telegram().inspectLaunch(), initData: secret, user: { first_name: secret }, url: secret };
  const diagnostic = loginDiagnostic(input, new Error(secret));
  assert.deepEqual(Object.keys(diagnostic).sort(), ['code', 'endpoint', 'httpStatus', 'initDataPresent', 'sdkPresent', 'stage', 'telegramContextHint'].sort());
  assert.ok(!JSON.stringify(diagnostic).includes(secret));
  const invalid = new ApiError(401, 'auth');
  Object.assign(invalid, { stage: secret, status: NaN });
  assert.equal(loginDiagnostic(input, invalid).httpStatus, null);
  assert.ok(!JSON.stringify(loginDiagnostic(input, invalid)).includes(secret));
});
test('a supported signed launch succeeds and clears an earlier diagnostic', async () => {
  let proof = '';
  const context = createTelegramAdapter(() => ({ initData: proof, platform: 'android', ready() {}, expand() {} }));
  const client = new ApiClient('/api', context.getInitData, (async (url) => response(String(url).endsWith('/auth/telegram')
    ? { access_token: 'synthetic_session_'.repeat(3), token_type: 'bearer', expires_in: 900 }
    : String(url).endsWith('/me') ? profile : status())) as typeof fetch);
  const controller = new AppController(client, context, lifecycle);
  await controller.refresh(); assert.equal(controller.getSnapshot().diagnostic?.code, 'init_data_missing');
  proof = secret;
  await controller.refresh();
  assert.equal(controller.getSnapshot().phase, 'ready');
  assert.equal(controller.getSnapshot().diagnostic, null);
});
