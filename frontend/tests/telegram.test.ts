import assert from 'node:assert/strict';
import test from 'node:test';
import { createTelegramAdapter } from '../src/core/telegram.ts';
import type { TelegramWebApp } from '../src/core/telegram.ts';

test('adapter initializes official API and forwards only raw initData', () => {
  let ready = 0; let expand = 0;
  const app = { initData: 'signed-raw-proof', platform: 'tdesktop', ready() { ready++; }, expand() { expand++; },
    get initDataUnsafe(): never { throw new Error('must never read unsafe data'); } };
  const telegram = createTelegramAdapter(() => app);
  telegram.initialize();
  assert.equal(ready, 1); assert.equal(expand, 1);
  assert.equal(telegram.getInitData(), 'signed-raw-proof');
  assert.equal(telegram.hasTelegramContext(), true);
});
test('normal browser has no identity even if official SDK creates an empty WebApp', () => {
  for (const app of [undefined, { initData: '', platform: 'unknown', ready() {}, expand() {} }]) {
    const telegram = createTelegramAdapter(() => app);
    assert.equal(telegram.getInitData(), ''); assert.equal(telegram.hasTelegramContext(), false);
  }
});
test('keyboard launch is distinguished from a browser but still has no signed identity', () => {
  const telegram = createTelegramAdapter(() => ({ initData: '', platform: 'android', ready() {}, expand() {} }));
  assert.equal(telegram.hasTelegramContext(), true); assert.equal(telegram.getInitData(), '');
});
test('Telegram activation listener is removed and invite links use openTelegramLink', () => {
  const calls: unknown[] = []; const callback = () => {};
  const app: TelegramWebApp = { initData: '', platform: 'tdesktop', ready() {}, expand() {},
    onEvent(event, handler) { calls.push([event, handler]); }, offEvent(event, handler) { calls.push([event, handler]); },
    openTelegramLink(url) { calls.push(url); } };
  const telegram = createTelegramAdapter(() => app);
  const stop = telegram.onActivated(callback); stop();
  assert.equal(telegram.openTelegramLink('https://t.me/+invite'), true);
  assert.deepEqual(calls, [['activated', callback], ['activated', callback], 'https://t.me/+invite']);
});
test('normal browser retains real anchor navigation instead of sending unsupported SDK events', () => {
  let calls = 0;
  const telegram = createTelegramAdapter(() => ({ initData: '', platform: 'unknown', ready() {}, expand() {},
    openLink() { calls++; }, openTelegramLink() { calls++; } }));
  assert.equal(telegram.openExternalLink('https://github.com/K35177'), false);
  assert.equal(telegram.openTelegramLink('https://t.me/test_bot?startapp'), false);
  assert.equal(calls, 0);
});
