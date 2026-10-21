import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import re
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, CallbackQuery, ChatJoinRequest, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup

from kernel.config import ConfigurationError, Settings
from kernel.access import AccessService
from kernel.backend_config import BackendSettings
from kernel.contact import ContactService, build_contact_router
from kernel.storage import Store
from kernel.transport import ChatPacingMiddleware


class SecretRedactingFormatter(logging.Formatter):
    def __init__(self, token: str):
        super().__init__('%(asctime)s %(levelname)s %(name)s: %(message)s')
        self.token = token

    def format(self, record):
        text = super().format(record).replace(self.token, '[REDACTED]')
        return re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{25,}\b', '[REDACTED]', text)


def moderation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ chmod +r', callback_data=f'approve:{user_id}'),
        InlineKeyboardButton(text='❌ deny', callback_data=f'decline:{user_id}'),
    ]])


async def register_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command='start', description='запуск'),
        BotCommand(command='menu', description='главное меню'),
        BotCommand(command='contact', description='написать владельцу'),
        BotCommand(command='cancel', description='выйти из режима переписки'),
    ])


async def join_request(request: ChatJoinRequest, bot: Bot, settings: Settings, access_service: AccessService | None = None):
    if request.chat.id != settings.root_group_id:
        return
    if access_service and await access_service.handle_join(request):
        return
    user = request.from_user
    await bot.send_message(
        settings.owner_id,
        f'🔑 Запрос доступа к root\nПользователь: {user.full_name}\n'
        f'Username: @{user.username or "—"}\nID: {user.id}',
        reply_markup=moderation_keyboard(user.id),
    )


async def moderate(callback: CallbackQuery, bot: Bot, settings: Settings):
    if not callback.from_user or callback.from_user.id != settings.owner_id:
        await callback.answer('Permission denied', show_alert=True)
        return
    if not callback.data:
        return
    action, raw_id = callback.data.split(':', 1)
    try:
        user_id = int(raw_id)
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await callback.answer('Invalid user ID', show_alert=True)
        return
    try:
        if action == 'approve':
            await bot.approve_chat_join_request(settings.root_group_id, user_id)
            result = f'✅ Access granted: {user_id}'
        else:
            await bot.decline_chat_join_request(settings.root_group_id, user_id)
            result = f'❌ Access denied: {user_id}'
    except TelegramAPIError as exc:
        logging.warning('Could not process join request: %s', type(exc).__name__)
        await callback.answer('Ошибка Telegram. Заявка могла быть уже обработана; проверь её состояние.', show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(result)
    await callback.answer()


def create_dispatcher(bot: Bot, settings: Settings, store: Store, backend_options=None) -> tuple[Dispatcher, ContactService]:
    access = AccessService(store, bot, settings, backend_options or BackendSettings.from_env())
    dp = Dispatcher(settings=settings, access_service=access, events_isolation=SimpleEventIsolation())
    service = ContactService(store, bot, settings)
    dp.chat_join_request.register(join_request)
    dp.callback_query.register(moderate, F.data.startswith(('approve:', 'decline:')))
    dp.callback_query.register(access.handle_callback, F.data.startswith('access:'))
    dp.include_router(build_contact_router(service))

    @dp.errors()
    async def error(event: ErrorEvent):
        logging.error('Update handling failed: update=%s error=%s',
                      event.update.update_id, type(event.exception).__name__)
        return True

    return dp, service


async def main():
    try:
        settings = Settings.from_env()
        backend_options = BackendSettings.from_env()
    except ConfigurationError as exc:
        # Configuration diagnostics are deliberately sanitized at the source.
        # Exit before creating the database, Bot/session, worker or polling.
        print(f'Ошибка конфигурации: {exc}', file=sys.stderr)
        raise SystemExit(2) from None
    handler = logging.StreamHandler()
    handler.setFormatter(SecretRedactingFormatter(settings.token))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.info('Kernel startup: pid=%s executable=%s source=%s ui=reply-keyboard-v2',
                 os.getpid(), sys.executable, Path(__file__).resolve())
    store = Store(settings.database_path)
    await asyncio.to_thread(store.initialize)
    bot = Bot(token=settings.token)
    bot.session.middleware(ChatPacingMiddleware())
    worker = None
    access_worker = None
    try:
        # Read-only API call. Never rename the bot or alter group settings.
        me = await asyncio.wait_for(bot.get_me(), timeout=20)
        logging.info('Telegram identity confirmed: name=%s username=@%s id=%s', me.full_name, me.username, me.id)
        await register_commands(bot)
        dp, contacts = create_dispatcher(bot, settings, store, backend_options)
        worker = asyncio.create_task(contacts.run(), name='contact-outbox')
        access_worker = asyncio.create_task(dp['access_service'].run(), name='access-outbox')
        await dp.start_polling(
            bot, allowed_updates=['message', 'chat_join_request', 'callback_query'],
            tasks_concurrency_limit=32, close_bot_session=False,
        )
    except (TelegramAPIError, TimeoutError) as exc:
        logging.error('Telegram connection failed: %s', type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        if access_worker:
            access_worker.cancel()
            with suppress(asyncio.CancelledError):
                await access_worker
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
