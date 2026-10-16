from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path

from .store import WebError, WebStore

logger = logging.getLogger(__name__)


def sales_location(root):
    from dotenv import dotenv_values
    path = root / os.getenv('AGENTBRIDGE_WEB_SALES_ENV_FILE','runtime/sales_buddy/.env')
    values = dotenv_values(path)
    chat = int(values.get('OWNER_SALES_BADDY_CHAT_ID') or '0')
    runtime = Path(values.get('SALES_BUDDY_RUNTIME_DIR') or root/'runtime/sales_buddy').resolve()
    if chat>=0 or not (runtime/'sales.sqlite3').is_file():
        raise ValueError('Configure the existing Sally env-file and database for Web reads')
    return runtime/'sales.sqlite3',chat


class WebRuntime:
    def __init__(self, root, product, service, settings):
        self.root, self.product, self.service, self.settings = root, product, service, settings
        self.store = WebStore(root / 'runtime/web/access.sqlite3')
        self.task = self.server = None

    async def start(self, app):
        self.app = app
        self.store.interrupted(self.product)
        if self.product == 'rick':
            from .data import WebData
            from .server import start_server
            sales_path,sales_chat = sales_location(self.root)
            data = WebData(self.store, self.service.registry, self.settings.database_path,
                           sales_path,sales_chat,self.settings.owner_chat_id)
            self.server = start_server(self.root, self.store, data)
        self.task = asyncio.create_task(self.run(), name='web-'+self.product)

    async def stop(self, app):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.server:
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()

    async def run(self):
        while True:
            claimed = await asyncio.to_thread(self.store.claim, self.product)
            if not claimed:
                await asyncio.sleep(.5)
                continue
            job, user = claimed
            try:
                if not self.store.allowed(user, self.product, job['context']):
                    raise WebError(403, 'Доступ к контексту отозван')
                payload = json.loads(job['payload'])
                if self.product == 'rick':
                    chat = self.service.registry.get(int(job['context']))
                    if not chat:
                        raise WebError(404, 'Чат недоступен')
                    async with self.service._chat_locks.setdefault(chat.telegram_chat_id, asyncio.Lock()):
                        if user['role'] == 'owner':
                            result = await self.service._answer_owner_query_for_chat(chat, payload['text'])
                        else:
                            # No owner thread, context file, tools or persistent member memory.
                            result = await self.service.provider.answer_web_question(
                                question=payload['text'], chat_name=chat.name,
                                context_pack=self.service._context_pack(chat))
                    self.store.finish(job['id'], 'done', result)
                else:
                    await self.sales(job, user, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=web_job_failed product=%s error_type=%s', self.product, type(exc).__name__)
                message = exc.message if isinstance(exc, WebError) else 'Не удалось завершить обработку. Проверьте историю перед повтором.'
                self.store.finish(job['id'], 'error', message)

    async def sales(self, job, user, payload):
        topic = int(job['context'])
        with self.service.store.connect() as db:
            deal = db.execute('SELECT title FROM deals WHERE chat_id=? AND topic_id=?', (self.settings.chat_id, topic)).fetchone()
        if not deal:
            raise WebError(404, 'Сделка недоступна')
        if self.service.store.awaiting_fact_review(self.settings.chat_id, topic):
            raise WebError(409, 'Сначала завершите проверку фактов в Telegram')
        # Negative IDs cannot collide with real Telegram updates/messages.
        synthetic = -1_000_000_000_000 - job['id']
        self.service.store.ingest(update_id=synthetic, chat_id=self.settings.chat_id,
            topic_id=topic, title=deal['title'], message_id=synthetic,
            author_id=-user['id'], author=user['name']+' · Web',
            date=datetime.now(timezone.utc).isoformat(), text=payload['text'], source='web_text', respond=True)
        # The existing analysis loop owns batching, locking, checkpoints and Telegram delivery.
        for _ in range(600):
            with self.service.store.connect() as db:
                event = db.execute('SELECT id,processed FROM events WHERE update_id=?', (synthetic,)).fetchone()
                if event['processed'] in (1,3):
                    rows = db.execute('SELECT event_ids,payload FROM recommendations WHERE chat_id=? AND topic_id=? ORDER BY id DESC LIMIT 50', (self.settings.chat_id,topic)).fetchall()
                    result = next((json.loads(r['payload'])['answer'] for r in rows if event['id'] in json.loads(r['event_ids'])), '')
                    self.store.finish(job['id'], 'done' if result else 'error', result or 'Материал сохранён, но ответ не подтверждён. Проверьте историю сделки.')
                    return
            if self.service.store.awaiting_fact_review(self.settings.chat_id, topic):
                self.store.finish(job['id'], 'review', 'Ответ ожидает подтверждения фактов в Telegram. Материал сохранён.')
                return
            await asyncio.sleep(1)
        self.store.finish(job['id'], 'uncertain', 'Материал сохранён в сделке. Обработка пока не завершена; проверьте историю позднее.')


def attach_web(app, root: Path, product, service, settings):
    from dotenv import dotenv_values
    values = {**dotenv_values(root/'.env'), **os.environ}
    if str(values.get('AGENTBRIDGE_WEB_ENABLED','')).lower() not in {'1','true','yes'}:
        return
    runtime = WebRuntime(root, product, service, settings)
    prior_start, prior_stop = app.post_init, app.post_stop

    async def start(application):
        if prior_start:
            await prior_start(application)
        await runtime.start(application)

    async def stop(application):
        await runtime.stop(application)
        if prior_stop:
            await prior_stop(application)

    app.post_init, app.post_stop = start, stop
