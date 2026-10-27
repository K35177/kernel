import assert from 'node:assert/strict';
import test from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { App } from '../src/App.tsx';
import { AppController } from '../src/core/controller.ts';
import { adapter, api, lifecycle, pending, status } from './helpers.ts';

const config = { apiBase: '/api', githubUrl: 'https://github.com/K35177', telegramAppUrl: null };
test('real React component renders Identity, configured GitHub and browser-only message', async () => {
  const telegram = adapter('', false); const controller = new AppController(api(), telegram, lifecycle);
  await controller.refresh(); const html = renderToStaticMarkup(<App controller={controller} telegram={telegram} config={config} />);
  for (const text of ['/home/tka', 'tka', 'developer / linux user / internet resident', 'GitHub',
    'https://github.com/K35177', 'Open this application through kernel in Telegram.']) assert.ok(html.includes(text));
  assert.ok(!html.includes('access: granted'));
});
test('real React component renders all access states and safely escapes user-provided names', async () => {
  for (const access of [status(), pending(),
    status({ state: 'awaiting_join', request_status: 'approved', request_label: null, next_action: 'open_invite',
      invite_url: 'https://t.me/+valid', invite_expires_at: Date.now() / 1000 + 1000 }),
    status({ state: 'member', access_label: 'access: granted', membership_confirmed: true }),
    status({ state: 'rejected', request_status: 'rejected' }),
    status({ state: 'verification_unavailable', membership_confirmed: null, membership_verification: 'unavailable' }),
  ]) {
    const telegram = adapter(); const backend = api(access);
    const original = backend.initialize;
    backend.initialize = async () => { const data = await original(); return { ...data, user: { ...data.user, display_name: '<script>private()</script>' } }; };
    const controller = new AppController(backend, telegram, lifecycle); await controller.refresh();
    const html = renderToStaticMarkup(<App controller={controller} telegram={telegram} config={config} />);
    assert.ok(html.includes('Root Access')); assert.ok(html.includes('Request access'));
    assert.ok(!html.includes('<script>private()'));
    if (access.request_status === 'approved') assert.ok(html.includes('Вступить в root'));
    if (access.membership_confirmed !== true) assert.ok(!html.includes('access: granted'));
  }
});
test('real React component contains confirmation with an explicit confirm and cancel step', async () => {
  const telegram = adapter(); const controller = new AppController(api(), telegram, lifecycle);
  await controller.refresh(); controller.confirmRequest();
  const html = renderToStaticMarkup(<App controller={controller} telegram={telegram} config={config} />);
  assert.ok(html.includes('<dialog')); assert.ok(html.includes('Подтвердить')); assert.ok(html.includes('Отмена'));
});
test('keyboard launch explains missing authorization and renders only safe diagnostics', async () => {
  const telegram = adapter('', true); const controller = new AppController(api(), telegram, lifecycle);
  await controller.refresh();
  const html = renderToStaticMarkup(<App controller={controller} telegram={telegram} config={config} />);
  assert.ok(html.includes('Telegram не передал данные для входа.'));
  assert.ok(html.includes('init_data_missing'));
  assert.ok(html.includes('Команды /start и /menu'));
  assert.ok(!html.includes('Request access'));
  assert.ok(!html.includes('access: granted'));
});
