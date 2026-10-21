export function telegramInvite(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.hostname !== 't.me' || url.port || url.username || url.password
        || url.search || url.hash || !/^\/(?:\+|joinchat\/)[A-Za-z0-9_-]+$/.test(url.pathname)) return null;
    return url.href;
  } catch { return null; }
}

export function telegramAppLink(value: string): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    const query = [...url.searchParams.keys()];
    if (url.protocol !== 'https:' || url.hostname !== 't.me' || url.port || url.username || url.password || url.hash
        || !/^\/[A-Za-z][A-Za-z0-9_]{4,31}(?:\/[A-Za-z0-9_]+)?$/.test(url.pathname)
        || !url.searchParams.has('startapp') || query.some((key) => key !== 'startapp')) return null;
    return url.href;
  } catch { return null; }
}

export function githubLink(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password && !url.port
      && !url.hash && !url.search && /^\/[A-Za-z0-9-]+\/?$/.test(url.pathname) ? url.href : null;
  } catch { return null; }
}

export function apiBase(value: string): string {
  if (value === '/api') return value;
  try {
    const url = new URL(value);
    if (url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash
        && url.pathname.replace(/\/$/, '') === '/api') return url.href.replace(/\/$/, '');
  } catch { /* A build configuration error is shown without its value. */ }
  throw new Error('VITE_API_BASE must be /api or an HTTPS API URL');
}
