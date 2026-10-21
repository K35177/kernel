"""Business logic shared by FastAPI and kernel. Approval is not membership."""
import asyncio
import logging
import re
import time

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from .access_store import AccessConflict, AccessStore
from .backend_config import BackendSettings

logger = logging.getLogger(__name__)


class MembershipUnavailable(RuntimeError):
    pass


class DeliveryPending(RuntimeError):
    pass


class AccessService:
    def __init__(self, store, bot, settings, options=None):
        self.repository = AccessStore(store)
        self.bot = bot
        self.settings = settings
        self.options = options or BackendSettings()

    async def db(self, method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    async def membership(self, user_id, *, force=False):
        previous = await self.db(self.repository.cached_membership, user_id)
        if not force and previous and previous['checked_at'] + self.options.membership_ttl > time.time():
            if previous['error_code']:
                raise MembershipUnavailable('Telegram membership verification unavailable')
            return bool(previous['is_member']), previous['telegram_status']
        try:
            member = await self.bot.get_chat_member(self.settings.root_group_id, user_id)
            status = str(member.status.value if hasattr(member.status, 'value') else member.status)
            confirmed = status in ('creator', 'administrator', 'member') or (
                status == 'restricted' and member.is_member)
        except TelegramAPIError as exc:
            await self.db(self.repository.membership, user_id, None, error=type(exc).__name__)
            raise MembershipUnavailable('Telegram membership verification unavailable') from None
        await self.db(self.repository.membership, user_id, bool(confirmed), status)
        return bool(confirmed), status

    async def status(self, user_id):
        try:
            member, _ = await self.membership(user_id)
            verification = 'verified'
        except MembershipUnavailable:
            member = None
            verification = 'unavailable'
        row = await self.db(self.repository.latest, user_id)
        request_status = row['status'] if row else 'not_submitted'
        if verification == 'unavailable':
            state = 'verification_unavailable'
        elif member:
            state = 'member'
        else:
            state = {'pending': 'pending', 'approved': 'awaiting_join', 'rejected': 'rejected'}.get(request_status, 'not_submitted')
        label = ('access: granted' if member is True else
                 'access: pending approval' if request_status == 'pending' else 'access: not granted')
        result = {
            'state': state, 'access_label': label,
            'request_label': 'request: not submitted' if not row else None,
            'request_id': row['id'] if row else None, 'request_status': request_status,
            'membership_confirmed': member, 'membership_verification': verification,
            'next_action': 'wait' if state == 'pending' else 'request_access' if state in ('not_submitted', 'rejected') else None,
            'invite_url': None, 'invite_expires_at': None,
        }
        if state == 'awaiting_join':
            invite = await self.db(self.repository.invite, row['id'])
            result['next_action'] = 'open_invite' if invite else 'wait_for_bot'
            if invite:
                result.update(invite_url=invite['invite_link'], invite_expires_at=invite['expires_at'])
        return result

    async def request(self, user_id):
        member, status = await self.membership(user_id, force=True)
        if member:
            return None, False
        if status == 'kicked':
            raise AccessConflict('banned')
        return await self.db(self.repository.request, user_id, self.options.reapply_cooldown)

    async def decide(self, request_id, decision, actor_id):
        if actor_id != self.settings.owner_id:
            raise PermissionError('Owner only')
        if not re.fullmatch(r'[a-f0-9]{32}', request_id):
            raise AccessConflict('not_found')
        return await self.db(self.repository.decide, request_id, decision, actor_id)

    async def handle_callback(self, callback):
        if (callback.from_user.id != self.settings.owner_id or not isinstance(callback.message, Message)
                or callback.message.chat.type != 'private' or callback.message.chat.id != self.settings.owner_id):
            await callback.answer('Доступ только владельцу.', show_alert=True)
            return
        match = re.fullmatch(r'access:(approve|decline):([a-f0-9]{32})', callback.data or '')
        if not match:
            await callback.answer('Некорректная заявка.', show_alert=True)
            return
        action, request_id = match.groups()
        if not await self.db(self.repository.callback_matches, request_id, callback.message.chat.id, callback.message.message_id):
            await callback.answer('Связь с заявкой не подтверждена. Повтори позднее.', show_alert=True)
            return
        row, changed = await self.decide(request_id, 'approved' if action == 'approve' else 'rejected', callback.from_user.id)
        await callback.answer('Решение сохранено. Уведомление в очереди.' if changed else 'Заявка уже обработана.')
        try:
            await callback.message.edit_text(
                'Заявка одобрена. Вступление подтверждается отдельно Telegram.' if row['status'] == 'approved'
                else 'Заявка обработана: ' + row['status'], reply_markup=None, parse_mode=None)
        except TelegramAPIError as exc:
            logger.warning('Access decision message edit failed: %s', type(exc).__name__)

    async def handle_join(self, request):
        if request.chat.id != self.settings.root_group_id or not request.invite_link:
            return False
        row = await self.db(self.repository.managed_invite, request.invite_link.invite_link)
        if not row:
            return False  # Existing manual join-request workflow remains active.
        if (row['user_id'] != request.from_user.id or row['status'] != 'approved'
                or row['revoked'] or row['expires_at'] <= time.time()):
            await self.bot.decline_chat_join_request(request.chat.id, request.from_user.id)
            return True
        await self.db(self.repository.queue_join, row['id'])
        return True

    async def notice(self, chat_id, text, **kwargs):
        return await self.bot.send_message(chat_id, text, parse_mode=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True), **kwargs)

    async def deliver(self, job):
        row = await self.db(self.repository.get, job['request_id'])
        kind = job['kind']
        if kind == 'owner_request':
            if row['status'] != 'pending':
                return None
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='Одобрить', callback_data='access:approve:' + row['id']),
                InlineKeyboardButton(text='Отклонить', callback_data='access:decline:' + row['id']),
            ]])
            sent = await self.notice(self.settings.owner_id,
                f"Заявка Mini App в root\nИмя: {row['full_name']}\nUsername: @{row['username'] or '—'}\n"
                f"Telegram ID: {row['user_id']}\nЗаявка: {row['id']}", reply_markup=keyboard)
            return self.settings.owner_id, sent.message_id
        if kind == 'user_decision':
            if row['status'] == 'rejected':
                await self.notice(row['user_id'], 'Заявка на доступ в root отклонена.')
                return None
            if row['status'] not in ('approved', 'joined'):
                return None
            member, telegram_status = await self.membership(row['user_id'], force=True)
            if member:
                await self.notice(row['user_id'], 'Доступ в root одобрен. Telegram подтвердил твоё членство.')
                return None
            if row['status'] == 'joined':
                return None  # Leaving root requires a new application.
            if telegram_status == 'kicked':
                raise DeliveryPending('banned')
            invite = await self.db(self.repository.invite, row['id'])
            if not invite:
                expires_at = int(time.time()) + self.options.invite_ttl
                created = await self.bot.create_chat_invite_link(
                    self.settings.root_group_id, name='kernel-' + row['id'][:16],
                    expire_date=expires_at, creates_join_request=True)
                # The link can only request entry. Acceptance still checks the
                # verified Telegram ID against this exact approved request.
                await self.db(self.repository.save_invite, row['id'], created.invite_link, expires_at)
                invite = {'invite_link': created.invite_link}
            await self.notice(row['user_id'], 'Заявка в root одобрена. Ты ещё не считаешься участником.\n'
                'Открой ссылку и отправь заявку на вступление — kernel проверит её автоматически:\n' + invite['invite_link'])
        elif kind == 'join_approval':
            if row['status'] not in ('approved', 'joined'):
                return None
            member, _ = await self.membership(row['user_id'], force=True)
            if not member:
                await self.bot.approve_chat_join_request(self.settings.root_group_id, row['user_id'])
                member, _ = await self.membership(row['user_id'], force=True)
                if not member:
                    raise DeliveryPending('membership_not_confirmed')
        elif kind == 'revoke_invite':
            while invite := await self.db(self.repository.invite, row['id'], include_expired=True):
                await self.bot.revoke_chat_invite_link(self.settings.root_group_id, invite['invite_link'])
                await self.db(self.repository.mark_revoked, invite['invite_link'])
        return None

    async def process_one(self):
        job = await self.db(self.repository.claim_job)
        if not job:
            return False
        try:
            notification = await self.deliver(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Only class names, never API exception bodies, URLs, token or initData.
            delay = max(1, exc.retry_after) if isinstance(exc, TelegramRetryAfter) else min(3600, 5 * 2**min(job['attempts'], 9))
            await self.db(self.repository.finish_job, job, error=type(exc).__name__, delay=delay)
            logger.warning('Access delivery deferred: kind=%s error=%s', job['kind'], type(exc).__name__)
        else:
            await self.db(self.repository.finish_job, job, notification=notification)
        return True

    async def run(self):
        while True:
            try:
                if await self.process_one():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('Access outbox delayed: %s', type(exc).__name__)
            await asyncio.sleep(1)
