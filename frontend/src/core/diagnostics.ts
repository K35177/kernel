/** Local, fixed-shape diagnostics. Never log, persist or transmit this report. */
import type { LaunchInspection } from './telegram.ts';
import { ApiError, NetworkError, SessionExpiredError, ResponseFormatError } from './types.ts';
import type { RequestEndpoint } from './types.ts';
export type DiagnosticCode = 'sdk_unavailable' | 'init_data_missing' | 'init_data_present'
  | 'auth_rejected' | 'auth_payload_rejected' | 'session_expired' | 'http_error'
  | 'transport_or_cors_error' | 'request_timeout' | 'invalid_json' | 'invalid_response' | 'unexpected_error';
export interface LoginDiagnostic extends LaunchInspection {
  code: DiagnosticCode;
  stage: 'launch' | 'auth' | 'api';
  httpStatus: number | null;
  endpoint: RequestEndpoint | 'none';
}
function safeEndpoint(value: unknown): RequestEndpoint {
  return (['telegram_auth', 'me', 'access_status', 'access_request'] as const).find(endpoint => endpoint === value) ?? 'unknown';
}
function safeStatus(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 100 && value <= 599 ? value : null;
}
export function loginDiagnostic(launch: LaunchInspection, error?: unknown): LoginDiagnostic {
  // Copy explicit booleans rather than spreading an SDK or caller-supplied object.
  const safe = { sdkPresent: launch.sdkPresent === true, initDataPresent: launch.initDataPresent === true,
    telegramContextHint: launch.telegramContextHint === true };
  const base: LoginDiagnostic = { ...safe, code: !safe.sdkPresent ? 'sdk_unavailable'
    : !safe.initDataPresent ? 'init_data_missing' : 'init_data_present', stage: 'launch', httpStatus: null, endpoint: 'none' };
  if (!error) return base;
  if (error instanceof SessionExpiredError) return { ...base, code: 'session_expired', stage: 'auth' };
  if (error instanceof ResponseFormatError) return { ...base, stage: error.stage === 'auth' ? 'auth' : 'api',
    endpoint: safeEndpoint(error.endpoint), httpStatus: safeStatus(error.httpStatus),
    code: error.kind === 'invalid_json' ? 'invalid_json' : 'invalid_response' };
  if (error instanceof ApiError) return { ...base, stage: error.stage === 'auth' ? 'auth' : 'api',
    endpoint: safeEndpoint(error.endpoint), httpStatus: safeStatus(error.status),
    code: error.stage === 'auth' && error.status === 401 ? 'auth_rejected'
      : error.stage === 'auth' && error.status === 422 ? 'auth_payload_rejected' : 'http_error' };
  // Browser fetch cannot reliably distinguish CORS, DNS, TLS, offline and timeouts.
  if (error instanceof NetworkError) return { ...base, code: error.timedOut === true ? 'request_timeout' : 'transport_or_cors_error',
    endpoint: safeEndpoint(error.endpoint), stage: error.stage === 'auth' ? 'auth' : 'api' };
  return { ...base, code: 'unexpected_error', stage: 'api' };
}
