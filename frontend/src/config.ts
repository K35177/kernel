import profile from '../config/profile.json';
import { apiBase, githubLink, telegramAppLink } from './core/links.ts';

export interface PublicConfig {
  apiBase: string;
  githubUrl: string | null;
  telegramAppUrl: string | null;
}
export const config: PublicConfig = {
  apiBase: apiBase(import.meta.env.VITE_API_BASE || '/api'),
  githubUrl: githubLink(import.meta.env.VITE_GITHUB_URL || profile.githubUrl),
  telegramAppUrl: telegramAppLink(import.meta.env.VITE_TELEGRAM_APP_URL || profile.telegramAppUrl),
};
