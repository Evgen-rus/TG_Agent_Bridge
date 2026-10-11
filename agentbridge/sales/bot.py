from __future__ import annotations

import asyncio
import logging
import re

from telegram.ext import Application, MessageHandler, filters
from agentbridge.telegram.media import message_text
from agentbridge.telegram.polling import HeartbeatHTTPXRequest, PollingHeartbeat

logger = logging.getLogger(__name__)
HELP = 'Я Сали. Создайте тему для сделки и отправьте предысторию или текст разговора. В этой теме я помню вашу сделку. Аудио и документы пока не разбираю — вставьте текст.'


class SalesTelegram:
    def __init__(self, chat_id, service):
        self.chat_id, self.service = chat_id, service
        self.tasks = []

    async def ingest(self, update, context):
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if (not message or not user or user.is_bot or not chat or chat.id != self.chat_id
                or chat.type != 'supergroup' or not chat.is_forum
                or getattr(message, 'sender_chat', None)):
            return
        topic = getattr(message, 'message_thread_id', None)
        if not topic or not getattr(message, 'is_topic_message', False):
            return
        # Membership is the user-authorized access policy; fail closed on API failures.
        try:
            member = await context.bot.get_chat_member(self.chat_id, user.id)
        except Exception:
            logger.warning('event=sales_membership_unavailable')
            return
        if member.status not in {'creator', 'administrator', 'member', 'restricted'}:
            return
        if member.status == 'restricted' and not getattr(member, 'is_member', False):
            return
        text = message_text(message)
        title = getattr(getattr(message, 'forum_topic_created', None), 'name', '')
        if title:
            text = f'Название сделки: {title}'
        if text.startswith('/'):
            if text.split()[0].split('@')[0] in {'/start', '/help'}:
                await asyncio.to_thread(self.service.store.ingest,
                    update_id=update.update_id, chat_id=self.chat_id, topic_id=topic,
                    title=title or f'Тема {topic}', message_id=message.message_id,
                    author_id=user.id, author=user.full_name, date=message.date.isoformat(),
                    text=text, source='command', respond=False, response_text=HELP)
            return
        if not text:
            if getattr(message, 'document', None) or getattr(message, 'voice', None) or getattr(message, 'audio', None):
                text = '[Вложение не разобрано. Содержимое неизвестно.]'
            else:
                return
        reply = getattr(message, 'reply_to_message', None)
        addressed_elsewhere = bool(reply and getattr(reply, 'from_user', None)
            and reply.from_user.id != context.bot.id)
        other_mentions = re.findall(r'@([A-Za-z0-9_]+)', text)
        mentions_sali = any(name.lower() == (context.bot.username or '').lower() for name in other_mentions)
        explicit = mentions_sali or bool(re.search(r'(?i)\bсали\b', text))
        quiet = bool(re.fullmatch(r'(?i)(спасибо|ок|окей|понял[аи]?|принято|\+)[.! ]*', text))
        respond = explicit or not (quiet or addressed_elsewhere or (other_mentions and not mentions_sali))
        await asyncio.to_thread(self.service.store.ingest,
            update_id=update.update_id, chat_id=self.chat_id, topic_id=topic,
            title=title or f'Тема {topic}', message_id=message.message_id,
            author_id=user.id, author=user.full_name, date=message.date.isoformat(),
            text=text, reply_to=getattr(reply, 'message_id', None), respond=respond,
            source='unsupported_attachment' if text.startswith('[Вложение') else 'telegram_text')

    async def analyze_pending(self, app):
        while True:
            try:
                topics = await asyncio.to_thread(self.service.store.pending_topics)
                for chat_id, topic_id in topics:
                    if chat_id == self.chat_id:
                        await self.service.process(chat_id, topic_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=sales_worker_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(2)

    async def deliver_pending(self, app):
        while True:
            try:
                blocked = set()
                for row in await asyncio.to_thread(self.service.store.deliveries):
                    if row['chat_id'] != self.chat_id or row['topic_id'] in blocked:
                        continue
                    try:
                        result = await app.bot.send_message(chat_id=self.chat_id,
                            message_thread_id=row['topic_id'], text=row['text'])
                        await asyncio.to_thread(self.service.store.delivered, row['id'], result.message_id)
                    except Exception as exc:
                        blocked.add(row['topic_id'])
                        logger.warning('event=sales_delivery_failed error_type=%s', type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=sales_delivery_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(3)

    async def start(self, app):
        chat = await app.bot.get_chat(self.chat_id)
        if chat.type != 'supergroup' or not chat.is_forum:
            raise ValueError('Sales Buddy chat must be a forum supergroup')
        member = await app.bot.get_chat_member(self.chat_id, app.bot.id)
        if member.status not in {'administrator', 'creator'}:
            raise ValueError('Sales Buddy must be a group administrator to receive natural messages')
        self.tasks = [asyncio.create_task(self.analyze_pending(app)),
                      asyncio.create_task(self.deliver_pending(app))]

    async def stop(self, app):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def create_sales_application(settings, service):
    adapter = SalesTelegram(settings.chat_id, service)
    app = (Application.builder().token(settings.token)
        .get_updates_request(HeartbeatHTTPXRequest(PollingHeartbeat(), 30))
        .post_init(adapter.start).post_stop(adapter.stop).build())
    app.add_handler(MessageHandler(filters.ALL, adapter.ingest))

    async def error(update, context):
        logger.warning('event=sales_telegram_failed error_type=%s', type(context.error).__name__)

    app.add_error_handler(error)
    return app
