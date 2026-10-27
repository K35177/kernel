import { apiBase } from './links.ts';
import {
  ApiError, NetworkError, NoTelegramContextError, SessionExpiredError, ResponseFormatError,
  parseAccess, parseProfile, parseRequest, parseSession,
} from './types.ts';
import type { AccessStatus, Profile, RequestResult, RequestEndpoint } from './types.ts';

function endpointFor(path: string): RequestEndpoint {
  switch (path) {
    case '/auth/telegram': return 'telegram_auth';
    case '/me': return 'me';
    case '/access/status': return 'access_status';
    case '/access/request': return 'access_request';
    default: return 'unknown';
  }
}

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
    // Browser-native fetch rejects an ApiClient receiver before any request.
    this.fetcher = fetcher.bind(globalThis);
    this.now = now;
  }

  private async json(path: string, method: string, token?: string, body?: unknown): Promise<unknown> {
    const stage = path === '/auth/telegram' ? 'auth' : 'api';
    const endpoint = endpointFor(path);
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 15000);
    try {
      const response = await this.fetcher(this.base + path, {
        method, signal: abort.signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
        headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...(token ? { Authorization: 'Bearer ' + token } : {}) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      if (!response.ok) throw new ApiError(response.status, stage, endpoint);
      try { return await response.json(); }
      catch (error) {
        if (error instanceof SyntaxError && !abort.signal.aborted) {
          throw new ResponseFormatError(stage, endpoint, 'invalid_json', response.status);
        }
        throw error;
      }
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new NetworkError(stage, endpoint, abort.signal.aborted);
    } finally { clearTimeout(timeout); }
  }

  private parse<T>(path: string, value: unknown, parser: (value: unknown) => T): T {
    try { return parser(value); }
    catch {
      // Retain only the known request identity, never response fields or parser errors.
      throw new ResponseFormatError(path === '/auth/telegram' ? 'auth' : 'api', endpointFor(path), 'invalid_response');
    }
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
        const data = this.parse('/auth/telegram', await this.json('/auth/telegram', 'POST', undefined, { initData: proof }), parseSession);
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
    const [user, access] = await Promise.all([
      this.personal('/me').then(value => this.parse('/me', value, parseProfile)),
      this.personal('/access/status').then(value => this.parse('/access/status', value, parseAccess)),
    ]);
    return { user, access };
  }
  async status(): Promise<AccessStatus> { return this.parse('/access/status', await this.personal('/access/status'), parseAccess); }
  async requestAccess(): Promise<RequestResult> { return this.parse('/access/request', await this.personal('/access/request', 'POST'), parseRequest); }
}
