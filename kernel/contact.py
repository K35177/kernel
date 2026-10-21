import asyncio
from datetime import datetime
import logging
import time
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
    TelegramNetworkError, TelegramRetryAfter, TelegramServerError,
)
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, KeyboardButton, ReplyKeyboardMarkup,
    LinkPreviewOptions, Message, ReplyParameters, WebAppInfo,
)

from .config import Settings
from .storage import Store

logger = logging.getLogger(__name__)
COPYABLE = frozenset({
    'text', 'photo', 'document', 'audio', 'video', 'voice', 'video_note',
    'animation', 'sticker', 'contact', 'location', 'venue', 'poll', 'dice',
})


def clip(text: str, units: int) -> str:
    """Telegram's length limit uses UTF-16 code units, not Python characters."""
    raw = text.encode('utf-16-le')
    if len(raw) <= units * 2:
        return text
    return raw[:(units - 1) * 2].decode('utf-16-le', errors='ignore') + '…'


def preview(message: Message) -> str:
    if message.text is not None:
        return message.text
    label = f'[{message.content_type.value}]'
    if message.document and message.document.file_name:
        label += ' ' + message.document.file_name
    if message.caption:
        label += '\n' + message.caption
    return label


def menu_keyboard(settings: Settings) -> ReplyKeyboardMarkup:
    home = (KeyboardButton(text='/home/tka', web_app=WebAppInfo(url=settings.mini_app_url))
            if settings.mini_app_url else
            KeyboardButton(text='/home/tka'))
    return ReplyKeyboardMarkup(
        keyboard=[[home], [KeyboardButton(text='Написать')]],
        resize_keyboard=True, is_persistent=True, one_time_keyboard=False,
    )


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text='/cancel'), KeyboardButton(text='/menu')]],
        resize_keyboard=True, is_persistent=True, one_time_keyboard=False,
    )


class ContactService:
    def __init__(self, store: Store, bot: Bot, settings: Settings):
        self.store = store
        self.bot = bot
        self.settings = settings
        self.wake = asyncio.Event()

    async def db(self, method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    async def send(self, chat_id: int, text: str, *,
                   reply_markup: ReplyKeyboardMarkup | None = None, **kwargs):
        if reply_markup is not None and not isinstance(reply_markup, ReplyKeyboardMarkup):
            raise TypeError('Contact navigation requires ReplyKeyboardMarkup')
        sent = await self.bot.send_message(
            chat_id, text, parse_mode=None,
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True), **kwargs)
        if reply_markup is not None:
            logger.info('Navigation keyboard sent: type=ReplyKeyboardMarkup rows=%s ui=reply-keyboard-v2',
                        len(reply_markup.keyboard))
        return sent

    async def safe_notice(self, chat_id: int, text: str, *,
                          reply_markup: ReplyKeyboardMarkup | None = None, **kwargs):
        try:
            return await self.send(chat_id, text, reply_markup=reply_markup, **kwargs)
        except TelegramAPIError as exc:
            # Never log an exception body: Bot API errors may include message data.
            logger.warning('Contact notice failed: %s', type(exc).__name__)
            return None

    async def menu(self, message: Message):
        user = message.from_user
        await self.db(self.store.set_contact, user.id, user.full_name, user.username, False)
        await self.safe_notice(message.chat.id, 'kernel\n/home/tka — визитка\nНаписать — связь с владельцем',
                               reply_markup=menu_keyboard(self.settings))

    async def open_contact(self, user, chat_id: int):
        await self.db(self.store.set_contact, user.id, user.full_name, user.username, True)
        if user.id == self.settings.owner_id:
            await self.safe_notice(chat_id, 'Для ответа пользователю нажми Reply на его уведомлении или вложении.\n'
                                   '/cancel или /menu — главное меню.', reply_markup=contact_keyboard())
            return
        await self.safe_notice(chat_id, 'Переписка открыта. Отправляй сообщения и вложения — они будут переданы владельцу.\n'
                               'Количество сообщений не ограничено; доставка выполняется по очереди.\n'
                               '/cancel или /menu — завершить переписку и открыть меню.',
                               reply_markup=contact_keyboard())

    async def home(self, user, chat_id: int):
        await self.db(self.store.set_contact, user.id, user.full_name, user.username, False)
        text = ('Открой /home/tka кнопкой на клавиатуре.' if self.settings.mini_app_url else
                '/home/tka готовится. Переписка уже доступна через «Написать».')
        await self.safe_notice(chat_id, text, reply_markup=menu_keyboard(self.settings))

    async def enqueue(self, message: Message, user_id: int, full_name: str,
                      username: str | None, direction: str):
        # Only a compact preview goes into the notification; the original message
        # is copied separately, retaining text, entities and all supported media.
        await self.db(self.store.enqueue,
            user_id=user_id, full_name=full_name, username=username, direction=direction,
            source_chat_id=message.chat.id, source_message_id=message.message_id,
            content_type=message.content_type.value, content_text=preview(message),
            payload_json=message.model_dump_json(exclude_none=True, exclude={
                'reply_to_message', 'external_reply', 'pinned_message',
            }), sent_at=message.date.timestamp())
        self.wake.set()

    async def incoming(self, message: Message):
        user = message.from_user
        if not await self.db(self.store.contact_active, user.id):
            await self.safe_notice(message.chat.id, 'Нажми «Написать» или используй /contact.',
                                   reply_markup=menu_keyboard(self.settings))
            return
        if message.content_type not in COPYABLE:
            await self.safe_notice(message.chat.id, 'Этот тип сообщения нельзя передать. Отправь текст или обычное вложение.')
            return
        await self.enqueue(message, user.id, user.full_name, user.username, 'inbound')

    async def owner_reply(self, message: Message):
        # Both chat and sender must be the configured owner. No group/forwarded
        # command, username, user-supplied ID or "last active user" fallback.
        if (not message.from_user or message.from_user.id != self.settings.owner_id
                or message.chat.type != 'private' or message.chat.id != self.settings.owner_id):
            return
        reply = message.reply_to_message
        if (not reply or reply.chat.id != self.settings.owner_id or not reply.from_user
                or reply.from_user.id != self.bot.id):
            await self.safe_notice(message.chat.id, 'Ответ не отправлен. Используй Reply на уведомлении или копии сообщения пользователя.')
            return
        target = await self.db(self.store.resolve_reply, message.chat.id, reply.message_id)
        if not target or target['user_id'] == self.settings.owner_id:
            await self.safe_notice(message.chat.id, 'Ответ не отправлен: у этого сообщения нет сохранённого адресата. Выбери уведомление переписки.')
            return
        if message.content_type not in COPYABLE:
            await self.safe_notice(message.chat.id, 'Этот тип ответа не поддерживается. Отправь текст или обычное вложение.')
            return
        await self.enqueue(message, target['user_id'], target['full_name'], target['username'], 'outbound')

    def timestamp(self, value: float) -> str:
        return datetime.fromtimestamp(value, ZoneInfo(self.settings.timezone)).strftime('%d.%m.%Y %H:%M:%S %Z')

    async def notification(self, row: dict) -> str:
        history = await self.db(self.store.history, row['user_id'], row['id'])
        username = '@' + row['username'] if row['username'] else '—'
        lines = [
            'kernel / contact',
            f"Имя: {clip(row['full_name'], 160)}",
            f'Username: {username}',
            f"Telegram ID: {row['user_id']}",
            f"Дата: {self.timestamp(row['sent_at'])}",
            '\nТекущее сообщение (полностью — отдельной копией):',
            clip(row['content_text'], 1500),
            '\nПоследние сообщения:',
        ]
        labels = {'queued': 'в очереди', 'notified': 'в очереди', 'notifying': 'отправляется',
                  'delivering': 'отправляется', 'failed': 'не доставлено', 'uncertain': 'доставка не подтверждена'}
        for item in history:
            who = 'владелец' if item['direction'] == 'outbound' else 'пользователь'
            status = labels.get(item['status'])
            suffix = f' [{status}]' if status else ''
            lines.append(f"{self.timestamp(item['sent_at'])} · {who}{suffix}\n{clip(item['content_text'], 230)}")
        if not history:
            lines.append('—')
        lines.append('\nОтвет: Reply на это уведомление или копию сообщения.')
        return clip('\n'.join(lines), 4000)

    async def process(self, row: dict):
        inbound = row['direction'] == 'inbound'
        notifying = inbound and row['status'] == 'queued'
        phase = 'notifying' if notifying else 'delivering'
        if not await self.db(self.store.claim, row['id'], row['status'], phase):
            return
        try:
            if notifying:
                note = await self.send(self.settings.owner_id, await self.notification(row))
                await self.db(self.store.finish, row['id'], 'notified',
                              mapping=(self.settings.owner_id, note.message_id, 'notification'))
                return
            target_id = self.settings.owner_id if inbound else row['user_id']
            copied = await self.bot.copy_message(
                chat_id=target_id, from_chat_id=row['source_chat_id'],
                message_id=row['source_message_id'],
            )
            mapping = (self.settings.owner_id, copied.message_id, 'copy') if inbound else None
            await self.db(self.store.finish, row['id'], 'delivered', mapping=mapping)
        except TelegramRetryAfter as exc:
            await self.db(self.store.finish, row['id'], row['status'],
                          retry_at=time.time() + max(exc.retry_after, 1), failure_code='rate_limit')
            return
        except TelegramForbiddenError:
            await self.failed(row, 'forbidden', uncertain=False)
            return
        except TelegramBadRequest:
            await self.failed(row, 'bad_request', uncertain=False)
            return
        except (TelegramNetworkError, TelegramServerError):
            await self.failed(row, 'transport', uncertain=True)
            return
        except TelegramAPIError:
            await self.failed(row, 'telegram', uncertain=True)
            return
        if not inbound:
            await self.safe_notice(self.settings.owner_id,
                f"Доставлено: {clip(row['full_name'], 160)} · ID {row['user_id']}",
                reply_parameters=ReplyParameters(message_id=row['source_message_id'], allow_sending_without_reply=True))

    async def failed(self, row: dict, code: str, *, uncertain: bool):
        status = 'uncertain' if uncertain else 'failed'
        await self.db(self.store.finish, row['id'], status, failure_code=code)
        logger.warning('Contact delivery %s: record=%s reason=%s', status, row['id'], code)
        if row['direction'] == 'outbound':
            detail = ('Telegram не подтвердил доставку. Сообщение могло дойти; проверь перед повторной отправкой.'
                      if uncertain else
                      'Сообщение не доставлено. Пользователь мог заблокировать бота, либо Telegram не разрешает копирование.')
            await self.safe_notice(self.settings.owner_id,
                f"ID {row['user_id']}: {detail}",
                reply_parameters=ReplyParameters(message_id=row['source_message_id'], allow_sending_without_reply=True))
        else:
            detail = ('Доставка последнего сообщения не подтверждена. Оно сохранено; автоматический повтор остановлен, чтобы избежать дубля.'
                      if uncertain else
                      'Последнее сообщение сохранено, но доставить его владельцу не удалось. Попробуй позднее или отправь текст вместо вложения.')
            await self.safe_notice(row['user_id'], detail,
                reply_parameters=ReplyParameters(message_id=row['source_message_id'], allow_sending_without_reply=True))

    async def run(self):
        recovered = await self.db(self.store.recover_interrupted)
        if recovered:
            logger.warning('Interrupted contact writes require review: count=%s', recovered)
            await self.safe_notice(self.settings.owner_id,
                f'После перезапуска осталось сообщений без подтверждения доставки: {recovered}. '
                'Автоматический повтор остановлен; сведения сохранены в SQLite.')
        while True:
            self.wake.clear()
            try:
                row = await self.db(self.store.next_pending)
                if row:
                    await self.process(row)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Keep in-flight writes ambiguous after a DB failure. The API may
                # already have sent the message; guessing a recipient is forbidden.
                logger.error('Contact worker error: %s', type(exc).__name__)
                try:
                    await self.db(self.store.recover_interrupted)
                except Exception as recovery_error:
                    logger.error('Contact recovery delayed: %s', type(recovery_error).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except TimeoutError:
                pass


def build_contact_router(service: ContactService) -> Router:
    router = Router(name='contact')
    private = (F.chat.type == 'private') & F.from_user

    @router.message(private, Command('start', 'menu', 'cancel'))
    async def menu(message: Message):
        await service.menu(message)

    @router.message(private, Command('contact'))
    async def contact(message: Message):
        await service.open_contact(message.from_user, message.chat.id)

    @router.message(private, F.text == '/home/tka')
    async def home(message: Message):
        await service.home(message.from_user, message.chat.id)

    @router.message(private, F.text == 'Написать',
                    (F.from_user.id != service.settings.owner_id) | ~F.reply_to_message)
    async def contact_text(message: Message):
        # An owner's Reply containing this word is an answer to the user.
        await service.open_contact(message.from_user, message.chat.id)

    @router.callback_query(F.data.in_({'menu:home', 'menu:contact'}))
    async def legacy_menu(callback: CallbackQuery):
        # Older messages may still contain inline menus. Migrate a clicked menu
        # to the reply keyboard without touching moderation buttons or mappings.
        if (not isinstance(callback.message, Message) or callback.message.chat.type != 'private'
                or callback.message.chat.id != callback.from_user.id):
            await callback.answer('Открой личный чат с ботом.', show_alert=True)
            return
        await callback.answer()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError as exc:
            logger.warning('Legacy menu cleanup failed: %s', type(exc).__name__)
        if callback.data == 'menu:contact':
            await service.open_contact(callback.from_user, callback.message.chat.id)
        else:
            await service.home(callback.from_user, callback.message.chat.id)

    @router.message(private, F.from_user.id == service.settings.owner_id)
    async def reply(message: Message):
        await service.owner_reply(message)

    @router.message(private)
    async def incoming(message: Message):
        await service.incoming(message)

    return router
