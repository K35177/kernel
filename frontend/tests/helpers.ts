import type { AccessStatus, Profile, RequestResult } from '../src/core/types.ts';
import type { KernelAPI } from '../src/core/api.ts';
import type { TelegramAdapter } from '../src/core/telegram.ts';
import type { Lifecycle } from '../src/core/controller.ts';

export const profile: Profile = { telegram_id: 123456, display_name: 'Test user', username: 'test_user' };
export function status(overrides: Partial<AccessStatus> = {}): AccessStatus {
  return {
    state: 'not_submitted', access_label: 'access: not granted', request_label: 'request: not submitted',
    request_id: null, request_status: 'not_submitted', membership_confirmed: false, membership_verification: 'verified',
    next_action: 'request_access', invite_url: null, invite_expires_at: null, ...overrides,
  };
}
export function pending(): AccessStatus {
  return status({ state: 'pending', request_status: 'pending', request_id: 'a'.repeat(32), request_label: null,
    access_label: 'access: pending approval', next_action: 'wait' });
}
export function adapter(raw = 'test-proof', context = true): TelegramAdapter {
  return { initialize() {}, getInitData: () => raw, hasTelegramContext: () => context,
    inspectLaunch: () => ({ sdkPresent: true, initDataPresent: Boolean(raw), telegramContextHint: context }),
    onActivated: () => () => {}, openTelegramLink: () => false, openExternalLink: () => false };
}
export function api(initial = status()): KernelAPI {
  return {
    initialize: async () => ({ user: profile, access: initial }),
    status: async () => initial,
    requestAccess: async (): Promise<RequestResult> => ({ ...pending(), created: true }),
  };
}
export const lifecycle: Lifecycle = { isVisible: () => true, onVisible: () => () => {}, every: () => () => {} };
export function response(body: unknown, code = 200): Response {
  return new Response(JSON.stringify(body), { status: code, headers: { 'Content-Type': 'application/json' } });
}
