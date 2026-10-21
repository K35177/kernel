import asyncio
from contextlib import asynccontextmanager
import logging

from aiogram import Bot
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from kernel.access import AccessService, MembershipUnavailable
from kernel.access_store import AccessConflict, ReplayError
from kernel.auth import AuthenticationError, validate_init_data
from kernel.backend_config import BackendSettings
from kernel.config import Settings
from kernel.storage import Store
from kernel.transport import ChatPacingMiddleware
from .safety import RequestSafetyMiddleware

bearer = HTTPBearer(auto_error=False)


class TelegramAuth(BaseModel):
    model_config = ConfigDict(extra='forbid')
    initData: str = Field(min_length=1, max_length=16384)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: str = Field(pattern=r'^(approved|rejected)$')


def create_app(settings=None, options=None, bot=None, store=None):
    # Calling the factory is explicit; importing this module reads no real .env
    # and never opens a database or starts a Telegram session.
    settings = settings or Settings.from_env()
    options = options or BackendSettings.from_env()
    store = store or Store(settings.database_path)
    owns_bot = bot is None

    @asynccontextmanager
    async def lifespan(app):
        from bot import SecretRedactingFormatter
        handler = logging.StreamHandler()
        handler.setFormatter(SecretRedactingFormatter(settings.token))
        # Existing logging handlers may belong to a host process; sanitize all
        # formatter output without logging requests, headers or authentication.
        for logger in (logging.getLogger('backend'), logging.getLogger('kernel.access')):
            logger.handlers = [handler]
            logger.propagate = False
        await asyncio.to_thread(store.initialize)
        telegram = bot or Bot(settings.token)
        if owns_bot:
            telegram.session.middleware(ChatPacingMiddleware())
        app.state.access = AccessService(store, telegram, settings, options)
        try:
            yield
        finally:
            if owns_bot:
                await telegram.session.close()

    app = FastAPI(title='kernel /home/tka API', version='2', lifespan=lifespan,
                  docs_url='/docs' if options.environment == 'development' else None,
                  redoc_url=None, openapi_url='/openapi.json' if options.environment == 'development' else None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(options.allowed_hosts))
    if options.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(options.allowed_origins),
            allow_credentials=False, allow_methods=['GET','POST'], allow_headers=['Authorization','Content-Type'])
    app.add_middleware(RequestSafetyMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Default validation output can include the submitted initData/token.
        return JSONResponse({'detail':'Некорректный формат запроса.'}, status_code=422)

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        logging.getLogger('backend').error('Backend request failed: %s', type(exc).__name__)
        return JSONResponse({'detail':'Внутренняя ошибка сервера.'}, status_code=500,
                            headers={'Cache-Control':'no-store'})

    async def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or credentials.scheme.lower() != 'bearer':
            raise HTTPException(401, 'Требуется авторизация.', headers={'WWW-Authenticate':'Bearer'})
        user = await request.app.state.access.db(request.app.state.access.repository.session_user, credentials.credentials)
        if not user:
            raise HTTPException(401, 'Сессия недействительна или истекла.', headers={'WWW-Authenticate':'Bearer'})
        return user

    async def owner(user=Depends(current_user)):
        if user['telegram_id'] != settings.owner_id:
            raise HTTPException(403, 'Доступ только владельцу.')
        return user

    @app.post('/api/auth/telegram')
    async def authenticate(body: TelegramAuth, request: Request):
        try:
            identity = validate_init_data(body.initData, settings.token, options.init_data_ttl)
            repo = request.app.state.access.repository
            token = await request.app.state.access.db(repo.exchange, identity, identity.fingerprint,
                                                      identity.proof_expires_at, options.session_ttl)
        except AuthenticationError:
            raise HTTPException(401, 'Недействительные или просроченные Telegram initData.') from None
        except ReplayError:
            raise HTTPException(409, 'Эти initData уже использованы. Используйте сессию или заново откройте Mini App.') from None
        return {'access_token':token, 'token_type':'bearer', 'expires_in':options.session_ttl,
                'user':{'telegram_id':identity.id, 'username':identity.username, 'display_name':identity.display_name}}

    @app.get('/api/me')
    async def me(request: Request, user=Depends(current_user)):
        state = await request.app.state.access.status(user['telegram_id'])
        return {'telegram_id':user['telegram_id'], 'username':user['username'], 'display_name':user['full_name'], **state}

    @app.get('/api/access/status')
    async def access_status(request: Request, user=Depends(current_user)):
        return await request.app.state.access.status(user['telegram_id'])

    @app.post('/api/access/request')
    async def access_request(request: Request, user=Depends(current_user)):
        service = request.app.state.access
        try:
            row, created = await service.request(user['telegram_id'])
        except MembershipUnavailable:
            raise HTTPException(503, 'Не удалось проверить членство в Telegram. Повторите позднее.') from None
        except AccessConflict as exc:
            raise HTTPException(403 if str(exc) == 'banned' else 409, 'Заявка сейчас недоступна.') from None
        return {'created':created, **await service.status(user['telegram_id'])}

    @app.get('/api/admin/access/requests')
    async def admin_requests(request: Request, user=Depends(owner)):
        return {'requests': await request.app.state.access.db(request.app.state.access.repository.list_requests)}

    @app.post('/api/admin/access/requests/{request_id}/decision')
    async def admin_decision(request_id: str, body: Decision, request: Request, user=Depends(owner)):
        try:
            row, changed = await request.app.state.access.decide(request_id, body.decision, user['telegram_id'])
        except AccessConflict:
            raise HTTPException(404, 'Заявка не найдена.') from None
        return {'request_id':row['id'], 'request_status':row['status'], 'changed':changed}

    @app.post('/api/admin/access/requests/{request_id}/retry')
    async def admin_retry(request_id: str, request: Request, user=Depends(owner)):
        repository = request.app.state.access.repository
        if len(request_id) != 32 or not await request.app.state.access.db(repository.get, request_id):
            raise HTTPException(404, 'Заявка не найдена.')
        changed = await request.app.state.access.db(repository.retry, request_id)
        return {'queued':bool(changed)}

    return app
