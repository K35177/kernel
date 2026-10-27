/** Minimal adapter for the official https://telegram.org/js/telegram-web-app.js.
 * Never reads initDataUnsafe. Only raw initData is sent to the backend.
 */
export interface TelegramWebApp {
  initData: string;
  platform?: string;
  ready(): void;
  expand(): void;
  openLink?(url: string): void;
  openTelegramLink?(url: string): void;
  onEvent?(event: 'activated', callback: () => void): void;
  offEvent?(event: 'activated', callback: () => void): void;
}
export interface TelegramAdapter {
  initialize(): void;
  getInitData(): string;
  hasTelegramContext(): boolean;
  inspectLaunch(): LaunchInspection;
  onActivated(callback: () => void): () => void;
  openTelegramLink(url: string): boolean;
  openExternalLink(url: string): boolean;
}
export interface LaunchInspection {
  sdkPresent: boolean;
  initDataPresent: boolean;
  telegramContextHint: boolean;
}

export function createTelegramAdapter(provider: () => TelegramWebApp | undefined): TelegramAdapter {
  const inTelegram = (app: TelegramWebApp | undefined): boolean => Boolean(app
    && ((typeof app.initData === 'string' && app.initData.length > 0)
      || (app.platform && app.platform !== 'unknown')));
  return {
    initialize() {
      try { provider()?.ready(); provider()?.expand(); } catch { /* Older clients still show the page. */ }
    },
    getInitData() {
      const raw = provider()?.initData;
      return typeof raw === 'string' ? raw : '';
    },
    hasTelegramContext() {
      return inTelegram(provider());
    },
    inspectLaunch() {
      const app = provider();
      // Presence only. Do not return proof, platform, URL fragment, user or SDK objects.
      return { sdkPresent: Boolean(app), initDataPresent: typeof app?.initData === 'string' && app.initData.length > 0,
        telegramContextHint: inTelegram(app) };
    },
    onActivated(callback) {
      const app = provider();
      try { app?.onEvent?.('activated', callback); } catch { /* visibilitychange remains available */ }
      return () => { try { app?.offEvent?.('activated', callback); } catch { /* no secret logging */ } };
    },
    openTelegramLink(url) {
      try {
        const app = provider();
        if (inTelegram(app) && app?.openTelegramLink) { app.openTelegramLink(url); return true; }
      } catch { /* Caller retains the normal anchor fallback. */ }
      return false;
    },
    openExternalLink(url) {
      try {
        const app = provider();
        if (inTelegram(app) && app?.openLink) { app.openLink(url); return true; }
      } catch { /* Caller retains the normal anchor fallback. */ }
      return false;
    },
  };
}
