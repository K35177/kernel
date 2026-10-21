"""Bounded pacing for all chat writes, including existing moderation notices."""
import asyncio
from collections import OrderedDict

from aiogram.client.session.middlewares.base import BaseRequestMiddleware


class ChatPacingMiddleware(BaseRequestMiddleware):
    def __init__(self, chat_interval: float = 1.05, global_interval: float = 0.04):
        self.chat_interval = chat_interval
        self.global_interval = global_interval
        self._global_at = 0.0
        self._chats: OrderedDict[int | str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def __call__(self, make_request, bot, method):
        # Reads, polling and callback answers must not wait behind chat traffic.
        chat_id = getattr(method, 'chat_id', None)
        name = method.__api_method__
        if chat_id is None or not name.startswith(('send', 'copy', 'forward', 'editMessage')):
            return await make_request(bot, method)
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                # Retain every unexpired reservation; prune only expired ones.
                while self._chats and next(iter(self._chats.values())) <= now:
                    self._chats.popitem(last=False)
                delay = max(self._global_at, self._chats.get(chat_id, 0)) - now
                if delay <= 0:
                    self._global_at = now + self.global_interval
                    self._chats[chat_id] = now + self.chat_interval
                    self._chats.move_to_end(chat_id)
                    break
            await asyncio.sleep(delay)
        return await make_request(bot, method)
