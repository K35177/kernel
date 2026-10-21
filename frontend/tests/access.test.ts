import assert from 'node:assert/strict';
import test from 'node:test';
import { accessView } from '../src/core/access-view.ts';
import { apiBase, githubLink, telegramAppLink, telegramInvite } from '../src/core/links.ts';
import { pending, status } from './helpers.ts';

test('initial and pending labels are retained', () => {
  assert.equal(accessView(status()).label, 'access: not granted');
  assert.equal(status().request_label, 'request: not submitted');
  assert.equal(accessView(pending()).label, 'access: pending approval');
  assert.equal(accessView(pending()).canRequest, false);
});
test('owner approval is waiting for join rather than membership', () => {
  const state = status({ state: 'awaiting_join', request_status: 'approved', next_action: 'open_invite', request_label: null,
    invite_url: 'https://t.me/+valid_invite', invite_expires_at: 200 });
  const view = accessView(state, 100000);
  assert.equal(view.label, 'access: not granted'); assert.equal(view.invite, 'https://t.me/+valid_invite');
  assert.match(view.description, /Владелец одобрил/); assert.equal(view.canRequest, false);
});
test('granted requires verified membership even if an inconsistent label arrives', () => {
  for (const [member, verification] of [[false, 'verified'], [null, 'unavailable']] as const) {
    assert.notEqual(accessView(status({ state: 'member', access_label: 'access: granted', membership_confirmed: member,
      membership_verification: verification })).label, 'access: granted');
  }
  assert.equal(accessView(status({ state: 'member', membership_confirmed: true })).label, 'access: granted');
});
test('rejection and Telegram API outage have distinct text', () => {
  assert.match(accessView(status({ state: 'rejected', request_status: 'rejected' })).description, /отклонена/);
  const view = accessView(status({ state: 'verification_unavailable', membership_confirmed: null, membership_verification: 'unavailable' }));
  assert.match(view.description, /Telegram API/); assert.equal(view.canRequest, false);
});
test('expired and malicious invite links cannot be opened', () => {
  const base = status({ state: 'awaiting_join', request_status: 'approved', invite_expires_at: 200, next_action: 'open_invite' });
  for (const link of ['javascript:alert(1)', 'https://evil.test/+invite', 'https://t.me.evil.test/+invite',
    'https://user:password@t.me/+invite', 'https://t.me/+invite?redirect=bad', 'http://t.me/+invite']) {
    assert.equal(accessView({ ...base, invite_url: link }, 100000).invite, null);
  }
  assert.equal(accessView({ ...base, invite_url: 'https://t.me/+valid' }, 201000).invite, null);
  assert.equal(accessView({ ...base, invite_url: 'https://t.me/+valid' }, 201000).canRenewInvite, true);
  assert.equal(telegramInvite('https://t.me/joinchat/valid'), 'https://t.me/joinchat/valid');
});
test('public configuration accepts the supplied GitHub and rejects unsafe links', () => {
  assert.equal(githubLink('https://github.com/K35177'), 'https://github.com/K35177');
  assert.equal(githubLink('https://github.com.evil.test/K35177'), null);
  assert.equal(telegramAppLink('https://t.me/test_bot?startapp'), 'https://t.me/test_bot?startapp');
  assert.equal(telegramAppLink('https://t.me/test_bot'), null);
  assert.equal(apiBase('/api'), '/api'); assert.throws(() => apiBase('http://evil.test/api'));
});
