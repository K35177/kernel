import { useEffect, useSyncExternalStore } from 'react';
import { accessView } from './core/access-view.ts';
import type { AppController } from './core/controller.ts';
import type { TelegramAdapter } from './core/telegram.ts';
import type { PublicConfig } from './config.ts';
import { ConfirmAccess } from './components/ConfirmAccess.tsx';

interface Props { controller: AppController; telegram: TelegramAdapter; config: PublicConfig }

export function App({ controller, telegram, config }: Props) {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  useEffect(controller.start, [controller]);
  const access = state.phase === 'ready' && state.access ? accessView(state.access) : null;
  const pending = state.busy !== null;
  const needsLaunch = ['outside-telegram', 'needs-launch', 'session-expired', 'unauthorized'].includes(state.phase);

  return <main className="page">
    <header className="identity">
      <p className="eyebrow">/home/tka</p>
      <h1>tka</h1>
      <p className="subtitle">developer / linux user / internet resident</p>
    </header>

    <section className="panel" aria-labelledby="links-title">
      <h2 id="links-title">Links</h2>
      {config.githubUrl ? <a className="link" href={config.githubUrl} target="_blank" rel="noopener noreferrer"
        onClick={(event) => { if (telegram.openExternalLink(config.githubUrl!)) event.preventDefault(); }}>GitHub <span aria-hidden="true">↗</span></a>
        : <p className="muted">GitHub пока не настроен.</p>}
    </section>

    <section className="panel" aria-labelledby="root-title" aria-busy={pending}>
      <h2 id="root-title">Root Access</h2>
      {state.phase === 'loading' && <p role="status">Загрузка и проверка авторизации…</p>}
      {state.phase === 'outside-telegram' && <>
        <p role="status">Open this application through kernel in Telegram.</p>
      </>}
      {state.phase === 'needs-launch' && <>
        <p role="status">Telegram не передал данные для входа.</p>
        <p className="muted">Кнопка на клавиатуре открывает визитку без авторизации. Для входа используйте
          настроенную Main Mini App в профиле kernel или Web App-кнопку меню рядом с полем ввода.
          Команды /start и /menu показывают ту же клавиатуру.</p>
      </>}
      {state.diagnostic?.code === 'sdk_unavailable' && <p className="muted">Telegram WebApp SDK недоступен.
        Проверьте соединение и откройте приложение заново.</p>}
      {state.error && <p className="error" role="alert">{state.error}</p>}
      {state.diagnostic && <details className="muted">
        <summary>Диагностика входа</summary>
        <p>Код: <code>{state.diagnostic.code}</code>; этап: {state.diagnostic.stage}.</p>
        <p>Запрос: {state.diagnostic.endpoint}.</p>
        <p>SDK: {state.diagnostic.sdkPresent ? 'доступен' : 'недоступен'};
          данные входа: {state.diagnostic.initDataPresent ? 'получены' : 'отсутствуют'}.</p>
        {state.diagnostic.httpStatus !== null && <p>HTTP: {state.diagnostic.httpStatus}</p>}
      </details>}
      {needsLaunch && config.telegramAppUrl && <a className="button" href={config.telegramAppUrl}
        onClick={(event) => { if (telegram.openTelegramLink(config.telegramAppUrl!)) event.preventDefault(); }}>Открыть Mini App в Telegram</a>}
      {state.phase !== 'outside-telegram' && state.phase !== 'loading' && state.phase !== 'ready' &&
        <button type="button" disabled={pending} onClick={() => void controller.refresh()}>{pending ? 'Проверка…' : 'Повторить проверку'}</button>}

      {access && <>
        {state.user && <p className="user">{state.user.display_name}{state.user.username ? ` · @${state.user.username}` : ''}</p>}
        <div className="status" aria-live="polite" aria-atomic="true">
          <p className="access-label">{access.label}</p>
          {state.access?.request_label && <p>{state.access.request_label}</p>}
          <p className="description">{access.description}</p>
        </div>
        {state.notice && <p className="notice" role="status">{state.notice}</p>}
        {access.invite && <a className="button primary" href={access.invite}
          onClick={(event) => { if (telegram.openTelegramLink(access.invite!)) event.preventDefault(); }}>Вступить в root</a>}
        <div className="actions">
          <button type="button" className="primary" disabled={pending || !access.canRequest}
            onClick={controller.confirmRequest}>Request access</button>
          <button type="button" disabled={pending} onClick={() => void controller.refresh()}>{state.busy === 'refresh' ? 'Обновление…' : 'Обновить статус'}</button>
        </div>
        {access.canRenewInvite && <button type="button" disabled={pending}
          onClick={() => void controller.renewInvite()}>Получить новую ссылку</button>}
      </>}
    </section>
    <footer>kernel / private space</footer>
    {state.confirmation && <ConfirmAccess busy={state.busy === 'submit'}
      onConfirm={() => void controller.submitRequest()} onCancel={controller.cancelConfirmation} />}
  </main>;
}
