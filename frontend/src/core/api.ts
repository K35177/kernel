import { apiBase } from './links.ts';
import {
  ApiError, NetworkError, NoTelegramContextError, SessionExpiredError,
  parseAccess, parseProfile, parseRequest, parseSession,
} from './types.ts';
import type { AccessStatus, Profile, RequestResult } from './types.ts';

export interface KernelAPI {
  initialize(): Promise<{ user: Profile; access: AccessStatus }>;
  status(): Promise<AccessStatus>;
  requestAccess(): Promise<RequestResult>;
}

export class ApiClient implements KernelAPI {
  private readonly base: string;
  private readonly getInitData: () => string;
  private readonly fetcher: typeof fetch;
  private readonly now: () => number;
  private session: { token: string; expiresAt: number } | null = null;
  private proofUsed: string | null = null;
  private authPending: Promise<string> | null = null;

  constructor(base: string, getInitData: () => string, fetcher: typeof fetch = fetch, now: () => number = Date.now) {
    this.base = apiBase(base);
    this.getInitData = getInitData;
    this.fetcher = fetcher;
    this.now = now;
  }

  private async json(path: string, method: string, token?: string, body?: unknown): Promise<unknown> {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 15000);
    try {
      const response = await this.fetcher(this.base + path, {
        method, signal: abort.signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
        headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...(token ? { Authorization: 'Bearer ' + token } : {}) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      if (!response.ok) throw new ApiError(response.status);
      try { return await response.json(); } catch { throw new ApiError(502); }
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new NetworkError();
    } finally { clearTimeout(timeout); }
  }

  private async authorized(): Promise<string> {
    if (this.session && this.session.expiresAt > this.now() + 1000) return this.session.token;
    if (this.authPending) return this.authPending;
    const proof = this.getInitData();
    if (!proof) throw new NoTelegramContextError();
    // The backend intentionally exchanges each proof only once. Expiration
    // cannot be fixed by repeatedly replaying an old Telegram initData string.
    if (proof === this.proofUsed) throw new SessionExpiredError();
    this.authPending = (async () => {
      try {
        const data = parseSession(await this.json('/auth/telegram', 'POST', undefined, { initData: proof }));
        this.proofUsed = proof;
        this.session = { token: data.access_token, expiresAt: this.now() + data.expires_in * 1000 };
        return data.access_token;
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) throw new SessionExpiredError();
        throw error;
      }
    })();
    try { return await this.authPending; } finally { this.authPending = null; }
  }

  private async personal(path: string, method = 'GET'): Promise<unknown> {
    const token = await this.authorized();
    try { return await this.json(path, method, token); }
    catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) throw error;
      if (this.session?.token === token) this.session = null;
      const renewed = await this.authorized();
      try { return await this.json(path, method, renewed); }
      catch (retryError) {
        if (retryError instanceof ApiError && retryError.status === 401) {
          this.session = null;
          throw new SessionExpiredError();
        }
        throw retryError;
      }
    }
  }

  async initialize(): Promise<{ user: Profile; access: AccessStatus }> {
    const [user, access] = await Promise.all([this.personal('/me'), this.personal('/access/status')]);
    return { user: parseProfile(user), access: parseAccess(access) };
  }
  async status(): Promise<AccessStatus> { return parseAccess(await this.personal('/access/status')); }
  async requestAccess(): Promise<RequestResult> { return parseRequest(await this.personal('/access/request', 'POST')); }
}
