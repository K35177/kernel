export type RequestStatus = 'not_submitted' | 'pending' | 'approved' | 'rejected' | 'joined';
export type AccessState = 'not_submitted' | 'pending' | 'awaiting_join' | 'member' | 'rejected' | 'verification_unavailable';
export type AccessLabel = 'access: not granted' | 'access: pending approval' | 'access: granted';

export interface AccessStatus {
  state: AccessState;
  access_label: AccessLabel;
  request_label: 'request: not submitted' | null;
  request_id: string | null;
  request_status: RequestStatus;
  membership_confirmed: boolean | null;
  membership_verification: 'verified' | 'unavailable';
  next_action: string | null;
  invite_url: string | null;
  invite_expires_at: number | null;
}

export interface Profile {
  telegram_id: number;
  username: string | null;
  display_name: string;
}

export interface RequestResult extends AccessStatus { created: boolean }
export interface SessionResponse {
  access_token: string;
  token_type: 'bearer';
  expires_in: number;
}

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number) {
    super('API request failed');
    this.name = 'ApiError';
    this.status = status;
  }
}
export class NetworkError extends Error {
  constructor() { super('Network unavailable'); this.name = 'NetworkError'; }
}
export class SessionExpiredError extends Error {
  constructor() { super('Reopen Mini App for fresh authorization'); this.name = 'SessionExpiredError'; }
}
export class NoTelegramContextError extends Error {
  constructor() { super('Telegram context unavailable'); this.name = 'NoTelegramContextError'; }
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError(502);
  return value as Record<string, unknown>;
}
function oneOf<T extends string>(value: unknown, choices: readonly T[]): T {
  if (typeof value !== 'string' || !choices.includes(value as T)) throw new ApiError(502);
  return value as T;
}
function optionalText(value: unknown): string | null {
  if (value !== null && typeof value !== 'string') throw new ApiError(502);
  return value;
}

export function parseAccess(value: unknown): AccessStatus {
  const v = object(value);
  if (![true, false, null].includes(v.membership_confirmed as boolean | null)
      || (v.invite_expires_at !== null && (typeof v.invite_expires_at !== 'number' || !Number.isFinite(v.invite_expires_at)))) {
    throw new ApiError(502);
  }
  return {
    state: oneOf(v.state, ['not_submitted', 'pending', 'awaiting_join', 'member', 'rejected', 'verification_unavailable']),
    access_label: oneOf(v.access_label, ['access: not granted', 'access: pending approval', 'access: granted']),
    request_label: v.request_label === null ? null : oneOf(v.request_label, ['request: not submitted'] as const),
    request_id: optionalText(v.request_id),
    request_status: oneOf(v.request_status, ['not_submitted', 'pending', 'approved', 'rejected', 'joined']),
    membership_confirmed: v.membership_confirmed as boolean | null,
    membership_verification: oneOf(v.membership_verification, ['verified', 'unavailable']),
    next_action: optionalText(v.next_action),
    invite_url: optionalText(v.invite_url),
    invite_expires_at: v.invite_expires_at as number | null,
  };
}

export function parseProfile(value: unknown): Profile {
  const v = object(value);
  if (!Number.isSafeInteger(v.telegram_id) || (v.telegram_id as number) <= 0 || typeof v.display_name !== 'string') {
    throw new ApiError(502);
  }
  return { telegram_id: v.telegram_id as number, display_name: v.display_name, username: optionalText(v.username) };
}

export function parseSession(value: unknown): SessionResponse {
  const v = object(value);
  if (typeof v.access_token !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(v.access_token)
      || v.token_type !== 'bearer' || typeof v.expires_in !== 'number'
      || !Number.isFinite(v.expires_in) || v.expires_in < 1 || v.expires_in > 3600) throw new ApiError(502);
  return { access_token: v.access_token, token_type: 'bearer', expires_in: v.expires_in };
}

export function parseRequest(value: unknown): RequestResult {
  const v = object(value);
  if (typeof v.created !== 'boolean') throw new ApiError(502);
  return { ...parseAccess(v), created: v.created };
}
