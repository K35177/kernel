"""ASGI request size/rate guard; independent of FastAPI and Telegram."""
from collections import OrderedDict
import json
import time


class RequestSafetyMiddleware:
    def __init__(self, app, max_body=20000, auth_per_minute=60):
        self.app = app
        self.max_body = max_body
        self.auth_per_minute = auth_per_minute
        self.clients = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/api/'):
            return await self.app(scope, receive, send)

        async def safe_send(message):
            if message['type'] == 'http.response.start':
                headers = [(k,v) for k,v in message.get('headers', []) if k.lower() not in (b'cache-control', b'x-content-type-options')]
                message = {**message,'headers':headers + [(b'cache-control',b'no-store'), (b'x-content-type-options',b'nosniff')]}
            await send(message)

        async def reject(status, detail):
            body = json.dumps({'detail':detail}, ensure_ascii=False).encode()
            await safe_send({'type':'http.response.start','status':status,
                             'headers':[(b'content-type',b'application/json; charset=utf-8')]})
            await safe_send({'type':'http.response.body','body':body})

        if scope.get('query_string'):
            return await reject(400, 'Query-параметры для API не поддерживаются.')
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
            if length < 0:
                raise ValueError
        except ValueError:
            return await reject(400, 'Некорректная длина запроса.')
        if length > self.max_body:
            return await reject(413, 'Запрос слишком большой.')
        if scope['path'] == '/api/auth/telegram':
            now = time.monotonic()
            client = (scope.get('client') or ('local',))[0]
            count, deadline = self.clients.get(client, (0, now + 60))
            if deadline <= now:
                count, deadline = 0, now + 60
            self.clients[client] = (count + 1, deadline)
            self.clients.move_to_end(client)
            while len(self.clients) > 1024:
                self.clients.popitem(last=False)
            if count >= self.auth_per_minute:
                return await reject(429, 'Слишком много попыток входа. Повторите позднее.')
        chunks, total = [], 0
        while True:
            chunk = await receive()
            if chunk['type'] == 'http.disconnect':
                return
            data = chunk.get('body', b'')
            total += len(data)
            if total > self.max_body:
                return await reject(413, 'Запрос слишком большой.')
            chunks.append(data)
            if not chunk.get('more_body', False):
                break
        consumed = False
        async def replay_body():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type':'http.request','body':b''.join(chunks),'more_body':False}
            return await receive()
        await self.app(scope, replay_body, safe_send)
