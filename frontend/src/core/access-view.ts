import { telegramInvite } from './links.ts';
import type { AccessStatus } from './types.ts';

export function accessView(status: AccessStatus, now = Date.now()) {
  const verified = status.membership_verification === 'verified';
  const granted = verified && status.membership_confirmed === true;
  const unavailable = !verified || status.membership_confirmed === null || status.state === 'verification_unavailable';
  const label = granted ? 'access: granted' : status.access_label === 'access: granted' ? 'access: not granted' : status.access_label;
  const invite = verified && !granted && status.request_status === 'approved'
    && status.invite_expires_at !== null && status.invite_expires_at * 1000 > now
    ? telegramInvite(status.invite_url) : null;
  let description: string;
  if (unavailable) description = 'Telegram API сейчас недоступен. Членство не подтверждено — обновите статус позднее.';
  else if (granted) description = 'Telegram подтвердил ваше членство в root.';
  else if (status.request_status === 'pending') description = 'Заявка отправлена. Ожидается решение владельца.';
  else if (status.request_status === 'approved') description = invite
    ? 'Владелец одобрил заявку. Откройте ссылку и отправьте запрос на вступление в Telegram. kernel проверит его автоматически.'
    : 'Владелец одобрил заявку, но членство ещё не подтверждено. Бот готовит приглашение; если ссылка истекла, запросите новую.';
  else if (status.request_status === 'rejected') description = 'Заявка отклонена. Повторная подача возможна после установленной паузы.';
  else description = 'Отправьте заявку на доступ в закрытую группу root.';
  return {
    label, description, invite,
    canRequest: verified && status.membership_confirmed === false && status.next_action === 'request_access',
    canRenewInvite: verified && status.membership_confirmed === false && status.request_status === 'approved' && !invite,
    shouldPoll: status.request_status === 'pending' || status.request_status === 'approved',
  };
}
