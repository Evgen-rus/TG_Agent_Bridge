"""Transient Telegram typing, shared by active jobs in the same deal topic."""

import asyncio
from contextlib import asynccontextmanager
import logging

logger = logging.getLogger(__name__)
TYPING_REFRESH_SECONDS = 4


class SalesActivity:
    def __init__(self):
        self.tasks = {}
        self.closed = False

    async def _loop(self, bot, chat_id, topic_id):
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, message_thread_id=topic_id or None, action='typing')
            except Exception:
                logger.warning('event=sales_typing_failed')
            await asyncio.sleep(TYPING_REFRESH_SECONDS)

    @asynccontextmanager
    async def scope(self, bot, chat_id, topic_id):
        if self.closed:
            yield
            return
        key = (chat_id, topic_id)
        entry = self.tasks.get(key)
        if entry is None:
            entry = [asyncio.create_task(self._loop(bot, chat_id, topic_id), name='sales-typing'), 0]
            self.tasks[key] = entry
        entry[1] += 1
        try:
            # Like Rick, do not wait for Telegram before starting model work.
            await asyncio.sleep(0)
            yield
        finally:
            entry[1] -= 1
            if not entry[1] and self.tasks.get(key) is entry:
                self.tasks.pop(key)
                entry[0].cancel()
                await asyncio.gather(entry[0], return_exceptions=True)

    async def stop(self):
        self.closed = True
        tasks = [entry[0] for entry in self.tasks.values()]
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
