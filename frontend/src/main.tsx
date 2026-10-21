import { createRoot } from 'react-dom/client';
import { App } from './App.tsx';
import { config } from './config.ts';
import { ApiClient } from './core/api.ts';
import { AppController } from './core/controller.ts';
import { createTelegramAdapter } from './core/telegram.ts';
import type { TelegramWebApp } from './core/telegram.ts';
import './styles.css';

declare global { interface Window { Telegram?: { WebApp?: TelegramWebApp } } }
const telegram = createTelegramAdapter(() => window.Telegram?.WebApp);
const api = new ApiClient(config.apiBase, telegram.getInitData);
const controller = new AppController(api, telegram, {
  isVisible: () => document.visibilityState === 'visible',
  onVisible(callback) {
    document.addEventListener('visibilitychange', callback);
    return () => document.removeEventListener('visibilitychange', callback);
  },
  every(callback, milliseconds) {
    const timer = window.setInterval(callback, milliseconds);
    return () => window.clearInterval(timer);
  },
});

const root = document.getElementById('root');
if (root) createRoot(root).render(<App controller={controller} telegram={telegram} config={config} />);
