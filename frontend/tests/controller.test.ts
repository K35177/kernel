import assert from 'node:assert/strict';
import test from 'node:test';
import { setImmediate as nextTick } from 'node:timers/promises';
import { AppController } from '../src/core/controller.ts';
import { ApiError, NetworkError, SessionExpiredError } from '../src/core/types.ts';
import type { RequestResult } from '../src/core/types.ts';
import { adapter, api, lifecycle, pending, status } from './helpers.ts';

test('normal browser shows Telegram-only state without inventing a user or making API calls', async () => {
  const backend = api(); let calls = 0;
  backend.initialize = async () => { calls++; throw new Error('must not initialize'); };
  const controller = new AppController(backend, adapter('', false), lifecycle);
  assert.equal(controller.getSnapshot().phase, 'loading');
  await controller.refresh();
  assert.equal(controller.getSnapshot().phase, 'outside-telegram');
  assert.equal(controller.getSnapshot().user, null); assert.equal(controller.getSnapshot().access, null); assert.equal(calls, 0);
});
test('keyboard without initData asks for supported launch rather than mock auth', async () => {
  const controller = new AppController(api(), adapter('', true), lifecycle);
  await controller.refresh(); assert.equal(controller.getSnapshot().phase, 'needs-launch');
  assert.equal(controller.getSnapshot().user, null);
});
test('signed context loads user and actual access status', async () => {
  const controller = new AppController(api(), adapter(), lifecycle);
  await controller.refresh();
  assert.equal(controller.getSnapshot().phase, 'ready');
  assert.equal(controller.getSnapshot().user?.telegram_id, 123456);
  assert.equal(controller.getSnapshot().access?.request_status, 'not_submitted');
});
test('request requires confirmation and double-clicks only submit once', async () => {
  const backend = api(); let submitted = 0; let finish!: (value: RequestResult) => void;
  backend.requestAccess = () => { submitted++; return new Promise((resolve) => { finish = resolve; }); };
  const controller = new AppController(backend, adapter(), lifecycle);
  await controller.refresh(); await controller.submitRequest(); assert.equal(submitted, 0);
  controller.confirmRequest(); assert.equal(controller.getSnapshot().confirmation, true);
  const first = controller.submitRequest(); const repeated = controller.submitRequest();
  assert.equal(submitted, 1); assert.equal(controller.getSnapshot().busy, 'submit');
  finish({ ...pending(), created: true }); await Promise.all([first, repeated]);
  assert.equal(controller.getSnapshot().access?.request_status, 'pending');
  assert.equal(controller.getSnapshot().confirmation, false);
  controller.confirmRequest(); await controller.submitRequest(); assert.equal(submitted, 1);
});
test('cancel confirmation sends nothing', async () => {
  let submitted = 0; const backend = api();
  backend.requestAccess = async () => { submitted++; return { ...pending(), created: true }; };
  const controller = new AppController(backend, adapter(), lifecycle);
  await controller.refresh(); controller.confirmRequest(); controller.cancelConfirmation(); await controller.submitRequest();
  assert.equal(submitted, 0);
});
test('idempotent backend response updates state without announcing a second application', async () => {
  const backend = api(); backend.requestAccess = async () => ({ ...pending(), created: false });
  const controller = new AppController(backend, adapter(), lifecycle);
  await controller.refresh(); controller.confirmRequest(); await controller.submitRequest();
  assert.match(controller.getSnapshot().notice!, /Новая заявка не создавалась/);
});
test('network failure clears stale granted access', async () => {
  const backend = api(status({ state: 'member', access_label: 'access: granted', membership_confirmed: true }));
  const controller = new AppController(backend, adapter(), lifecycle); await controller.refresh();
  backend.status = async () => { throw new NetworkError(); };
  await controller.refresh(); assert.equal(controller.getSnapshot().phase, 'network-error');
  assert.equal(controller.getSnapshot().access, null);
});
test('unauthorized, expired session and Telegram API failures are distinguishable', async () => {
  for (const [error, phase, fragment] of [
    [new ApiError(401), 'unauthorized', /авторизация отклонена/],
    [new SessionExpiredError(), 'session-expired', /Сессия истекла/],
    [new ApiError(503), 'network-error', /Telegram API/],
  ] as const) {
    const backend = api(); backend.initialize = async () => { throw error; };
    const controller = new AppController(backend, adapter(), lifecycle); await controller.refresh();
    assert.equal(controller.getSnapshot().phase, phase); assert.match(controller.getSnapshot().error!, fragment);
    assert.equal(controller.getSnapshot().access, null);
  }
});
test('manual refresh and visibility/activated events update access; polling pauses when hidden', async () => {
  let visible = true; let refreshes = 0; let onVisible!: () => void; let onActivated!: () => void; let poll!: () => void; let interval = 0;
  let cleaned = 0; const backend = api(pending());
  backend.status = async () => { refreshes++; return pending(); };
  const telegram = adapter(); telegram.onActivated = (callback) => { onActivated = callback; return () => { cleaned++; }; };
  const controller = new AppController(backend, telegram, {
    isVisible: () => visible,
    onVisible(callback) { onVisible = callback; return () => { cleaned++; }; },
    every(callback, milliseconds) { poll = callback; interval = milliseconds; return () => { cleaned++; }; },
  });
  const stop = controller.start(); await nextTick();
  assert.equal(interval, 30000);
  poll(); await nextTick(); assert.equal(refreshes, 1);
  visible = false; poll(); onVisible(); await nextTick(); assert.equal(refreshes, 1);
  visible = true; onVisible(); await nextTick(); onActivated(); await nextTick(); assert.equal(refreshes, 3);
  stop(); assert.equal(cleaned, 3);
});
test('expired invite can be renewed through existing idempotent request endpoint', async () => {
  const initial = status({ state: 'awaiting_join', request_status: 'approved', next_action: null, invite_url: null });
  const backend = api(initial); let calls = 0;
  backend.requestAccess = async () => { calls++; return { ...initial, created: false }; };
  const controller = new AppController(backend, adapter(), lifecycle); await controller.refresh();
  await controller.renewInvite(); assert.equal(calls, 1); assert.match(controller.getSnapshot().notice!, /Приглашение запрошено/);
});
