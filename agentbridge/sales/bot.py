from __future__ import annotations

import asyncio
import logging
import re
import json
import time
from dataclasses import replace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
from agentbridge.telegram.media import message_text, describe_message_media
from agentbridge.media import is_visual_media
from agentbridge.telegram.polling import HeartbeatHTTPXRequest, PollingHeartbeat, PollingWatchdog
from .operations import restart_sales
from .audio import AudioWorker, AUDIO_EXTENSIONS
from .images import ImageWorker
from .activity import SalesActivity
from .storage import SALI_NAME, should_respond
from .commitments import Commitments, delivery_allowed, disable

logger = logging.getLogger(__name__)
HELP = 'Я Сали. Создайте тему для сделки и отправьте предысторию, текст разговора, .txt/.md в UTF-8 (до 2 МБ), фото или изображение файлом (JPEG/PNG/WEBP/GIF до 20 МБ), голосовое или аудиозапись (до 20 МБ). В этой теме я помню вашу сделку. PDF/DOCX пока не разбираю. Администратор может запросить /restart с подтверждением.'
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024


def member_allowed(member):
    return member.status in {'creator', 'administrator', 'member'} or (
        member.status == 'restricted' and getattr(member, 'is_member', False))


class SalesTelegram:
    def __init__(self, chat_id, service, system_topic_id=0, heartbeat=None, audio_worker=None, image_worker=None):
        self.chat_id, self.service = chat_id, service
        self.tasks = []
        self.system_topic_id = system_topic_id
        self.heartbeat = heartbeat or PollingHeartbeat()
        self.run_id = None
        self.restart_tasks = set()
        self.delivery_lock = asyncio.Lock()
        self.stopping = False
        self.restart_verify_seconds = 15
        self.service.system_chat_id = chat_id
        self.service.system_topic_id = system_topic_id
        self.audio_worker = audio_worker
        self.image_worker = image_worker
        self.activity = SalesActivity()

    async def ingest(self, update, context):
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if (not message or not user or user.is_bot or not chat or chat.id != self.chat_id
                or chat.type != 'supergroup' or not chat.is_forum
                or getattr(message, 'sender_chat', None)):
            return
        topic = getattr(message, 'message_thread_id', None)
        topic = topic or 0
        # Membership is the user-authorized access policy; fail closed on API failures.
        authorization_pending = False
        try:
            member = await context.bot.get_chat_member(self.chat_id, user.id)
        except Exception:
            authorization_pending = True
            logger.warning('event=sales_membership_deferred')
        else:
            if not member_allowed(member):
                return
        text = message_text(message)
        command = text.split()[0].partition('@') if text.startswith('/') else ('', '', '')
        restart_requested = (command[0] == '/restart' and (not command[1] or
            command[2].lower() == (context.bot.username or '').lower())) or bool(
            re.fullmatch(rf'(?i)\s*{SALI_NAME}[,! ]+перезапустись[.! ]*', text))
        is_edit = getattr(update, "edited_message", None) is not None
        if is_edit and (restart_requested or text.startswith("/")):
            return
        if restart_requested:
            if authorization_pending or member.status not in {'administrator', 'creator'}:
                return
            await asyncio.to_thread(self.service.store.request_restart,
                self.chat_id, topic, user.id, update.update_id)
            return
        if not topic or not getattr(message, 'is_topic_message', False):
            return
        title = getattr(getattr(message, 'forum_topic_created', None), 'name', '')
        if title:
            text = f'Название сделки: {title}'
        if text.startswith('/'):
            command, separator, target = text.split()[0].partition('@')
            if separator and target.lower() != (context.bot.username or '').lower():
                return
            if command in {'/start', '/help'}:
                await asyncio.to_thread(self.service.store.ingest,
                    update_id=update.update_id, chat_id=self.chat_id, topic_id=topic,
                    title=title or f'Тема {topic}', message_id=message.message_id,
                    author_id=user.id, author=user.full_name, date=message.date.isoformat(),
                    text=text, source='command', respond=False, response_text=HELP,
                    authorization_pending=authorization_pending)
            return
        source = 'telegram_text'
        document = getattr(message, 'document', None)
        audio = getattr(message, 'voice', None) or getattr(message, 'audio', None)
        if not audio and document and any((getattr(document, 'file_name', '') or '').lower().endswith(ext) for ext in AUDIO_EXTENSIONS):
            audio = document
        metadata = None
        image = None
        ref = describe_message_media(message)
        photos = getattr(message, 'photo', None) or ()
        if photos and ref:
            best = max(photos, key=lambda item: (getattr(item, 'width', 0) * getattr(item, 'height', 0), getattr(item, 'file_size', 0) or 0))
            ref = replace(ref, file_id=best.file_id, file_size=getattr(best, 'file_size', None))
        if audio:
            metadata = dict(file_id=audio.file_id, file_size=getattr(audio, 'file_size', None),
                filename=getattr(audio, 'file_name', None) or ('voice.ogg' if getattr(message, 'voice', None) else 'audio'),
                mime=getattr(audio, 'mime_type', '') or '',
                kind='voice' if getattr(message, 'voice', None) else 'audio',
                duration=getattr(audio, 'duration', None))
            source = 'audio_pending'
        elif ref and is_visual_media(ref.kind, ref.mime, ref.filename):
            image = dict(file_id=ref.file_id, file_size=ref.file_size,
                filename=ref.filename, mime=ref.mime, media_group_id=ref.media_group_id, caption=text)
            source = 'image_pending'
        elif document and (document.file_name or '').lower().endswith(('.txt', '.md')):
            if await asyncio.to_thread(self.service.store.event_seen, update.update_id):
                return
            text = json.dumps({'file_id': document.file_id, 'size': document.file_size,
                'caption': text, 'filename': document.file_name}, ensure_ascii=False)
            source = 'text_document_pending'
        elif any(getattr(message, kind, None) for kind in ('document', 'voice', 'audio', 'photo', 'video')):
            text = '[Вложение не разобрано. Содержимое неизвестно.]' + ('\n' + text if text else '')
            source = 'unsupported_attachment'
        if not text and not metadata and not image:
            return
        reply = getattr(message, 'reply_to_message', None)
        addressed_elsewhere = bool(reply and getattr(reply, 'from_user', None)
            and reply.message_id != topic
            and reply.from_user.id != context.bot.id)
        routing = dict(bot_username=context.bot.username or '', addressed_elsewhere=addressed_elsewhere)
        respond = should_respond(text, **routing)
        if metadata is not None:
            metadata['routing'] = routing
            respond = True  # Decide from the caption and transcript after STT.
        await asyncio.to_thread(self.service.store.ingest,
            update_id=update.update_id, chat_id=self.chat_id, topic_id=topic,
            title=title or f'Тема {topic}', message_id=message.message_id,
            author_id=user.id, author=user.full_name,
            date=((getattr(message, "edit_date", None) or message.date) if is_edit else message.date).isoformat(),
            text=text, reply_to=getattr(reply, 'message_id', None), respond=respond,
            authorization_pending=authorization_pending,
            source=source, audio=metadata, image=image, is_edit=is_edit)

    async def hydrate_documents(self, bot):
        for row in await asyncio.to_thread(self.service.store.awaiting_documents, self.chat_id):
            retry_key = f"document-retry:{row['id']}"
            if (await asyncio.to_thread(self.service.store.op_get, retry_key, 0) or 0) > time.time():
                continue
            document = {}
            try:
                document = json.loads(row['text'])
                if not document['size'] or document['size'] > MAX_TEXT_FILE_BYTES:
                    raise ValueError('size')
                remote = await bot.get_file(document['file_id'])
                if remote.file_size and remote.file_size > MAX_TEXT_FILE_BYTES:
                    raise ValueError('size')
                payload = await remote.download_as_bytearray()
                if len(payload) > MAX_TEXT_FILE_BYTES:
                    raise ValueError('size')
                body = bytes(payload).decode('utf-8-sig')
                if '\x00' in body or not body.strip():
                    raise ValueError('encoding')
            except (UnicodeError, ValueError):
                text = '[Текстовый файл не прочитан: требуется UTF-8, непустой текст и размер до 2 МБ.]'
                source = 'unsupported_attachment'
            except Exception as exc:
                logger.warning('event=sales_document_deferred error_type=%s', type(exc).__name__)
                await asyncio.to_thread(self.service.store.op_set, retry_key, time.time() + 60)
                continue
            else:
                text = (document['caption'] + '\n\n' if document['caption'] else '') + body
                source = 'text_document'
            await asyncio.to_thread(self.service.store.op_set, f"document-source:{row['id']}", document)
            await asyncio.to_thread(self.service.store.hydrate_document, row['id'], text, source)
            await asyncio.to_thread(self.service.store.op_set, retry_key, None)

    async def callback(self, update, context):
        query = update.callback_query
        if query and (query.data or '').startswith('sales_fact:'):
            await self.fact_review_callback(update, context)
            return
        query, user, chat = update.callback_query, update.effective_user, update.effective_chat
        if not query or not user or user.is_bot or not chat or chat.id != self.chat_id:
            return
        try:
            member = await context.bot.get_chat_member(self.chat_id, user.id)
        except Exception:
            await query.answer('Не удалось проверить права. Попробуйте позже.', show_alert=True)
            return
        if member.status not in {'administrator', 'creator'}:
            await query.answer('Перезапуск доступен администраторам.', show_alert=True)
            return
        match = re.fullmatch(r'sales_restart:(\d+)', query.data or '')
        if not match:
            return
        marker = int(match.group(1))
        row = await asyncio.to_thread(self.service.store.get_restart, marker)
        message = getattr(query, 'message', None)
        if (not row or row['chat_id'] != self.chat_id or not message
                or (getattr(message, 'message_thread_id', None) or 0) != row['topic_id']
                or message.message_id != row.get('confirmation_message_id')):
            await query.answer('Подтверждение недоступно.')
            return
        if not await asyncio.to_thread(self.service.store.claim_restart, marker, user.id):
            await query.answer('Подтверждение уже использовано или недоступно.')
            return
        try:
            await query.answer('Запускаю перезапуск Сали.')
        except Exception as exc:
            logger.warning('event=sales_restart_callback_answer_failed error_type=%s', type(exc).__name__)
        await asyncio.to_thread(self.service.store.notice, self.chat_id, self.system_topic_id,
            f'restart-start:{marker}', 'Запускаю подтверждённый перезапуск Сали.')
        task = asyncio.create_task(self.launch_restart(marker, context.bot))
        self.restart_tasks.add(task)
        task.add_done_callback(self.restart_tasks.discard)

    async def launch_restart(self, marker, bot=None):
        try:
            if bot:
                await self.deliver_once(bot)
            await asyncio.to_thread(self.service.store.finish_restart, marker, 'launched')
            await asyncio.to_thread(restart_sales)
        except Exception as exc:
            logger.warning('event=sales_restart_failed error_type=%s', type(exc).__name__)
            await asyncio.to_thread(self.service.store.finish_restart, marker, 'failed')
            await asyncio.to_thread(self.service.store.notice, self.chat_id, self.system_topic_id,
                f'restart-failed:{marker}', 'Перезапуск Сали не разрешён или не удался. Сервис продолжает работу; проверьте отдельное разрешение systemd.')
            return
        await asyncio.sleep(self.restart_verify_seconds)
        if not self.stopping:
            await asyncio.to_thread(self.service.store.finish_restart, marker, 'failed')
            await asyncio.to_thread(self.service.store.notice, self.chat_id, self.system_topic_id,
                f'restart-unconfirmed:{marker}', 'Запрос перезапуска Сали принят, но остановка процесса не подтверждена. Сервис продолжает работать; проверьте systemd.')

    async def fact_review_callback(self, update, context):
        query, user, chat = update.callback_query, update.effective_user, update.effective_chat
        message = getattr(query, 'message', None)
        match = re.fullmatch(r'sales_fact:(accept|reject):(\d+)', query.data or '')
        if (not match or not user or user.is_bot or not chat or chat.id != self.chat_id or not message
                or getattr(message, 'sender_chat', None)):
            await query.answer('Подтверждение недоступно.', show_alert=True)
            return
        try:
            allowed = member_allowed(await context.bot.get_chat_member(self.chat_id, user.id))
        except Exception:
            await query.answer('Не удалось проверить права. Попробуйте позже.', show_alert=True)
            return
        if not allowed:
            await query.answer('Решение доступно участникам группы.', show_alert=True)
            return
        status = await asyncio.to_thread(self.service.store.decide_fact_review,
            int(match[2]), self.chat_id, getattr(message, 'message_thread_id', None) or 0,
            message.message_id, user.id, match[1] == 'accept')
        await query.answer({'accepted': 'Подтверждено.', 'rejected': 'Отклонено.',
            'stale': 'Запись изменилась; подтверждение устарело.',
            'already': 'Решение уже принято.', 'denied': 'Подтверждение недоступно.'}[status])
        if status != 'denied':
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as exc:
                logger.warning('event=sales_review_markup_failed error_type=%s', type(exc).__name__)

    async def authorize_pending(self, bot):
        permissions = {}
        for row in await asyncio.to_thread(self.service.store.awaiting_authorization, self.chat_id):
            author = row['author_id']
            if author not in permissions:
                try:
                    permissions[author] = member_allowed(await bot.get_chat_member(self.chat_id, author))
                except Exception:
                    permissions[author] = None
            if permissions[author] is not None:
                await asyncio.to_thread(self.service.store.authorize, row['id'], permissions[author], HELP)

    async def analyze_pending(self, app):
        while True:
            try:
                await self.authorize_pending(app.bot)
                await self.hydrate_documents(app.bot)
                if not self.service.available():
                    await self.service.probe()
                topics = await asyncio.to_thread(self.service.store.pending_topics, batched=True)
                for chat_id, topic_id in topics:
                    if chat_id == self.chat_id:
                        await self.service.process(chat_id, topic_id,
                            activity=lambda chat, topic: self.activity.scope(app.bot, chat, topic), batch=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=sales_worker_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(2)

    async def transcribe_pending(self, app):
        while True:
            try:
                await self.audio_worker.run_once(app.bot, self.chat_id,
                    activity=lambda chat, topic: self.activity.scope(app.bot, chat, topic))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=sales_audio_worker_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(2)

    async def read_images_pending(self, app):
        while True:
            try:
                await self.image_worker.run_once(app.bot, self.chat_id,
                    activity=lambda chat, topic: self.activity.scope(app.bot, chat, topic))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=sales_image_worker_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(2)

    async def deliver_pending(self, app):
        while True:
            try:
                await self.deliver_once(app.bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=sales_delivery_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(3)

    async def monitor_commitments(self):
        while True:
            try:
                await self.service.commitments.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=sales_commitments_worker_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(60)

    async def deliver_once(self, bot):
        async with self.delivery_lock:
            await self._deliver_once(bot)

    async def _deliver_once(self, bot):
        blocked = set()
        for row in await asyncio.to_thread(self.service.store.deliveries):
            if row['chat_id'] != self.chat_id or row['topic_id'] in blocked:
                continue
            try:
                if not await asyncio.to_thread(delivery_allowed, self.service.store, row, self.service.commitments is not None):
                    continue
                options = {}
                if row['delivery_key'].startswith('reminder:'):
                    options['parse_mode'] = 'HTML'
                if row['delivery_key'].startswith('fact-review:'):
                    marker = row['delivery_key'].split(':')[1]
                    options['reply_markup'] = InlineKeyboardMarkup([[
                        InlineKeyboardButton('Подтвердить', callback_data=f'sales_fact:accept:{marker}'),
                        InlineKeyboardButton('Отклонить', callback_data=f'sales_fact:reject:{marker}')]])
                if row['delivery_key'].startswith('restart-confirm:'):
                    marker = row['delivery_key'].split(':')[1]
                    options['reply_markup'] = InlineKeyboardMarkup([[InlineKeyboardButton(
                        'Перезапустить', callback_data=f'sales_restart:{marker}')]])
                result = await bot.send_message(chat_id=self.chat_id,
                    message_thread_id=row['topic_id'] or None, text=row['text'], **options)
                await asyncio.to_thread(self.service.store.delivered, row['id'], result.message_id)
            except Exception as exc:
                blocked.add(row['topic_id'])
                logger.warning('event=sales_delivery_failed error_type=%s', type(exc).__name__)

    async def start(self, app):
        chat = await app.bot.get_chat(self.chat_id)
        if chat.type != 'supergroup' or not chat.is_forum:
            raise ValueError('Sales Buddy chat must be a forum supergroup')
        member = await app.bot.get_chat_member(self.chat_id, app.bot.id)
        if member.status not in {'administrator', 'creator'}:
            raise ValueError('Sales Buddy must be a group administrator to receive natural messages')
        self.run_id = await asyncio.to_thread(self.service.store.begin_run, self.chat_id, self.system_topic_id)
        watchdog = PollingWatchdog(self.heartbeat, 10, 90, 20,
            ['message', 'edited_message', 'callback_query'], 0, fatal_state_key='sales_polling_fatal')
        self.tasks = [asyncio.create_task(self.analyze_pending(app)),
                      asyncio.create_task(self.deliver_pending(app)),
                      asyncio.create_task(self.startup_probe(app)),
                      asyncio.create_task(watchdog.run(app))]
        if self.audio_worker:
            self.tasks.append(asyncio.create_task(self.transcribe_pending(app)))
        if self.image_worker:
            self.tasks.append(asyncio.create_task(self.read_images_pending(app)))
        if self.service.commitments:
            self.tasks.append(asyncio.create_task(self.monitor_commitments()))

    async def startup_probe(self, app):
        while not app.running or (getattr(app, 'updater', None) is not None and not app.updater.running):
            await asyncio.sleep(.1)
        if await asyncio.to_thread(self.service.store.op_get, f'startup-probed:{self.run_id}'):
            return
        await asyncio.to_thread(self.service.store.notice, self.chat_id, self.system_topic_id,
            f'startup:{self.run_id}:telegram', 'Сали запущена. Telegram работает. Проверяю Codex.')
        try:
            prior = await asyncio.to_thread(self.service.store.op_get, 'codex_failure', {}) or {}
            ready = (await self.service.probe(force=True) if prior.get('kind') == 'codex_turn_failed'
                     else await self.service.probe())
        except Exception:
            ready = False
        state = await asyncio.to_thread(self.service.store.op_get, 'codex_failure', {}) or {}
        reason = {'codex_usage_limit': 'исчерпан лимит Codex', 'codex_auth': 'ошибка авторизации',
            'codex_network': 'сетевая ошибка', 'codex_transport_closed': 'недоступен транспорт Codex',
            'codex_sandbox_missing': 'недоступна песочница Codex',
            'codex_turn_failed': 'ошибка модели или некорректный ответ'}.get(state.get('kind'), 'доступ не подтверждён')
        text = ('Codex отвечает. Сали готова к работе.' if ready else
                f'Сали запущена, Telegram работает, но Codex сейчас недоступен. Причина: {reason}. Данные сделок сохранены; анализ возобновится после проверки доступа.')
        await asyncio.to_thread(self.service.store.notice, self.chat_id, self.system_topic_id,
            f'startup:{self.run_id}:codex', text)
        await asyncio.to_thread(self.service.store.op_set, f'startup-probed:{self.run_id}', True)

    async def stop(self, app):
        self.stopping = True
        await self.activity.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.run_id and not app.bot_data.get('sales_polling_fatal'):
            await asyncio.to_thread(self.service.store.end_run, self.run_id)


def create_sales_application(settings, service):
    if settings.commitments_enabled:
        service.commitments = Commitments(service, settings.chat_id)
    else:
        service.commitments = None
        disable(service.store)
    heartbeat = PollingHeartbeat()
    audio_worker = AudioWorker(service.store, settings.runtime / 'audio', settings.openai_api_key, settings.transcription_model)
    image_worker = ImageWorker(service, settings.runtime / 'images')
    adapter = SalesTelegram(settings.chat_id, service, settings.system_topic_id, heartbeat, audio_worker, image_worker)
    app = (Application.builder().token(settings.token)
        .get_updates_request(HeartbeatHTTPXRequest(heartbeat, 30))
        .post_init(adapter.start).post_stop(adapter.stop).build())
    app.add_handler(MessageHandler(filters.ALL, adapter.ingest))
    app.add_handler(CallbackQueryHandler(adapter.callback, pattern=r'^(?:sales_restart:|sales_fact:)'))

    async def error(update, context):
        logger.warning('event=sales_telegram_failed error_type=%s', type(context.error).__name__)

    app.add_error_handler(error)
    return app
