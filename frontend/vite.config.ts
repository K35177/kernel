import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv } from 'vite';
import { apiBase, githubLink, telegramAppLink } from './src/core/links.ts';

const root = fileURLToPath(new URL('.', import.meta.url));
const proxy = { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } };

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, root, 'VITE_');
  apiBase(env.VITE_API_BASE || '/api');
  if (env.VITE_GITHUB_URL && !githubLink(env.VITE_GITHUB_URL)) throw new Error('Invalid VITE_GITHUB_URL');
  if (env.VITE_TELEGRAM_APP_URL && !telegramAppLink(env.VITE_TELEGRAM_APP_URL)) throw new Error('Invalid VITE_TELEGRAM_APP_URL');
  return {
  root,
  // Never load the existing Python project's .env into the frontend build.
  envDir: root,
  envPrefix: 'VITE_',
  esbuild: { jsx: 'automatic' },
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
  preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
  build: { outDir: 'dist', sourcemap: false },
  };
});
