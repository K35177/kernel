import type { KernelAPI } from './api.ts';
import type { TelegramAdapter } from './telegram.ts';
import type { AccessStatus, Profile } from './types.ts';
import { ApiError, NetworkError, NoTelegramContextError, SessionExpiredError } from './types.ts';
import { accessView } from './access-view.ts';

export type Phase = 'loading' | 'outside-telegram' | 'needs-launch' | 'ready' | 'unauthorized' | 'session-expired' | 'network-error';
export interface AppState {
  phase: Phase;
  user: Profile | null;
  access: AccessStatus | null;
  busy: 'initial' | 'refresh' | 'submit' | null;
  confirmation: boolean;
  notice: string | null;
  error: string | null;
}
export interface Lifecycle {
  isVisible(): boolean;
  onVisible(callback: () => void): () => void;
  every(callback: () => void, ms: number): () => void;
}

export class AppController {
  private readonly api: KernelAPI;
  private readonly telegram: TelegramAdapter;
  private readonly lifecycle: Lifecycle;
  private listeners = new Set<() => void>();
  private running = false;
  private state: AppState = {
    phase: 'loading', user: null, access: null, busy: null, confirmation: false, notice: null, error: null,
  };

  constructor(api: KernelAPI, telegram: TelegramAdapter, lifecycle: Lifecycle) {
    this.api = api;
    this.telegram = telegram;
    this.lifecycle = lifecycle;
  }
  getSnapshot = (): AppState => this.state;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  private update(patch: Partial<AppState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }

  start = (): (() => void) => {
    if (this.running) return () => {};
    this.running = true;
    this.telegram.initialize();
    void this.refresh();
    const visible = this.lifecycle.onVisible(() => { if (this.lifecycle.isVisible()) void this.refresh(); });
    const activated = this.telegram.onActivated(() => { if (this.lifecycle.isVisible()) void this.refresh(); });
    const polling = this.lifecycle.every(() => {
      if (this.state.phase === 'ready' && this.state.access && accessView(this.state.access).shouldPoll
          && !this.state.confirmation && this.lifecycle.isVisible()) void this.refresh();
    }, 30000);
    return () => { this.running = false; visible(); activated(); polling(); };
  };

  private fail(error: unknown, submission = false) {
    let phase: Phase = 'network-error';
    let text = submission
      ? 'Нет связи с сервером. Заявка могла сохраниться — обновите статус перед повторной попыткой.'
      : 'Не удалось связаться с сервером. Проверьте соединение и повторите попытку.';
    if (error instanceof SessionExpiredError) {
      phase = 'session-expired'; text = 'Сессия истекла. Закройте приложение и заново откройте его через меню kernel или Main Mini App.';
    } else if (error instanceof NoTelegramContextError) {
      phase = this.telegram.hasTelegramContext() ? 'needs-launch' : 'outside-telegram';
      text = 'Open this application through kernel in Telegram.';
    } else if (error instanceof ApiError) {
      if (error.status === 401) { phase = 'unauthorized'; text = 'Telegram-авторизация отклонена. Заново откройте приложение через меню kernel.'; }
      else if (error.status === 403) text = 'Сервер не разрешил эту операцию. Обновите статус доступа.';
      else if (error.status === 409) text = 'Операция сейчас недоступна. Обновите статус или повторите позднее.';
      else if (error.status === 429) text = 'Слишком много запросов. Подождите немного и повторите попытку.';
      else if (error.status === 503) text = 'Telegram API сейчас недоступен. Заявка не подтверждена; проверьте статус позднее.';
      else text = 'Сервер не смог обработать запрос. Повторите попытку позднее.';
    } else if (!(error instanceof NetworkError)) {
      text = 'Не удалось загрузить данные приложения. Повторите попытку.';
    }
    this.update({ phase, busy: null, access: null, confirmation: false, error: text, notice: null,
      ...(phase === 'session-expired' || phase === 'unauthorized' ? { user: null } : {}) });
  }

  refresh = async (): Promise<void> => {
    if (this.state.busy) return;
    if (!this.telegram.getInitData()) {
      this.update({ phase: this.telegram.hasTelegramContext() ? 'needs-launch' : 'outside-telegram',
        busy: null, user: null, access: null, confirmation: false, error: null });
      return;
    }
    this.update({ busy: this.state.user ? 'refresh' : 'initial', error: null });
    try {
      if (!this.state.user) {
        const data = await this.api.initialize();
        this.update({ phase: 'ready', user: data.user, access: data.access, busy: null, notice: null });
      } else {
        const access = await this.api.status();
        this.update({ phase: 'ready', access, busy: null, notice: null });
      }
    } catch (error) { this.fail(error); }
  };

  confirmRequest = (): void => {
    if (this.state.phase === 'ready' && !this.state.busy && this.state.access && accessView(this.state.access).canRequest) {
      this.update({ confirmation: true, error: null, notice: null });
    }
  };
  cancelConfirmation = (): void => { if (this.state.busy !== 'submit') this.update({ confirmation: false }); };

  submitRequest = async (): Promise<void> => {
    if (!this.state.confirmation || this.state.busy || !this.state.access || !accessView(this.state.access).canRequest) return;
    await this.submit(false);
  };
  renewInvite = async (): Promise<void> => {
    if (this.state.phase !== 'ready' || this.state.busy || !this.state.access || !accessView(this.state.access).canRenewInvite) return;
    await this.submit(true);
  };
  private async submit(invite: boolean) {
    this.update({ busy: 'submit', error: null });
    try {
      const result = await this.api.requestAccess();
      this.update({ phase: 'ready', access: result, confirmation: false, busy: null,
        notice: invite ? 'Приглашение запрошено. Ссылка появится после обработки ботом; обновите статус позднее.'
          : result.created ? 'Заявка сохранена и ожидает решения владельца.'
            : 'Сервер вернул текущую заявку или подтверждённый доступ. Новая заявка не создавалась.' });
    } catch (error) { this.fail(error, true); }
  }
}
