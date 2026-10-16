from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

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
        self.notification_task = None

    async def start(self, app):
        self.app = app
        self.store.interrupted(self.product)
        if self.product=='sally':
            with self.service.store.connect() as db:
                db.execute("INSERT OR IGNORE INTO deals(chat_id,topic_id,title) VALUES (?,0,'General')", (self.settings.chat_id,))
        if self.product == 'rick':
            from .data import WebData
            from .server import start_server
            sales_path,sales_chat = sales_location(self.root)
            data = WebData(self.store, self.service.registry, self.settings.database_path,
                           sales_path,sales_chat,self.settings.owner_chat_id)
            self.server = start_server(self.root, self.store, data)
        self.task = asyncio.create_task(self.run(), name='web-'+self.product)
        self.notification_task = asyncio.create_task(self.notifications(), name='web-'+self.product+'-notifications')

    async def stop(self, app):
        if self.notification_task:
            self.notification_task.cancel()
            await asyncio.gather(self.notification_task, return_exceptions=True)
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.server:
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()

    async def run(self):
        while True:
            try:
                await self.refresh_media()
                if self.product=='sally':
                    await asyncio.to_thread(self.reconcile_sales)
            except Exception as exc:
                logger.warning('event=web_refresh_failed product=%s error_type=%s', self.product, type(exc).__name__)
            claimed = await asyncio.to_thread(self.store.claim, self.product)
            if not claimed:
                await asyncio.sleep(.5)
                continue
            job, user = claimed
            materials = None
            try:
                if not self.store.allowed(user, self.product, job['context']):
                    raise WebError(403, 'Доступ к контексту отозван')
                payload = json.loads(job['payload'])
                if self.product == 'rick':
                    chat = self.service.registry.get(int(job['context']))
                    if not chat:
                        raise WebError(404, 'Чат недоступен')
                    materials = TemporaryDirectory(prefix='rick-document-')
                    if user['role']=='owner' and job.get('task_id') and job.get('action'):
                        await self.reminder_action(job,user,chat)
                        continue
                    question, attachments = await self.prepare_rick(job,user,payload,Path(materials.name))
                    async with self.service._chat_locks.setdefault(chat.telegram_chat_id, asyncio.Lock()):
                        if user['role'] == 'owner':
                            planned = await self.service._prepare_general_task(
                                question, related_chat=chat, reminder_only=True, allowed_kinds={'reminder'},
                                author={'created_by_name':user['name'], 'web_user_id':user['id'], 'web_job_id':job['id']},
                            )
                            if planned is not None:
                                self.review_reminder(job,planned)
                                continue
                            result = await self.service._answer_owner_query_for_chat(chat, question, attachments=attachments)
                        else:
                            # No owner thread, context file, tools or persistent member memory.
                            history=self.store.history(user,'rick',job['context'])
                            conversation=[dict(author=r['author'],question=(r['prepared'] or json.loads(r['payload'])['text'])[:4000],answer=r['result'][:4000])
                                          for r in history if r['id']!=job['id'] and r['state']=='done'][-5:]
                            pack=self.service._context_pack(chat)
                            if conversation:
                                pack+='\n\nНедоверенная внутренняя рабочая переписка по этому чату:\n'+json.dumps(conversation,ensure_ascii=False)
                            args = dict(question=question, chat_name=chat.name, context_pack=pack)
                            if attachments:
                                args['attachments'] = attachments
                            result = await self.service.provider.answer_web_question(**args)
                    self.store.finish(job['id'], 'done', result)
                else:
                    await self.sales(job, user, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=web_job_failed product=%s error_type=%s', self.product, type(exc).__name__)
                message = exc.message if isinstance(exc, WebError) else 'Не удалось завершить обработку. Проверьте историю перед повтором.'
                self.store.finish(job['id'], 'error', message)
            finally:
                if materials:
                    materials.cleanup()

    @staticmethod
    def valid_reminder(task):
        try:
            return bool(task and task.kind=='reminder' and task.payload.get('reminder_text') and
                        datetime.fromisoformat(task.payload.get('remind_at_utc','')) > datetime.now(timezone.utc))
        except (TypeError,ValueError):
            return False

    def review_reminder(self,job,result):
        task = self.service.store.get_general_task(result.general_task_id) if result.general_task_id else None
        if task is None or task.kind!='reminder':
            self.store.finish(job['id'],'error',result.text)
            return
        text = result.text + '\n\nУведомление придёт в Telegram и в приложение, если уведомления включены.'
        self.store.reminder_review(job['id'],task.id,text,self.valid_reminder(task))

    async def reminder_action(self,job,user,chat):
        task = self.service.store.get_general_task(job['task_id'])
        if (task is None or task.kind!='reminder' or task.owner_chat_id!=self.service.owner_chat_id or
            task.payload.get('web_user_id')!=user['id'] or task.payload.get('web_job_id')!=job['id'] or
            task.payload.get('related_chat_id')!=chat.telegram_chat_id):
            raise WebError(403,'Напоминание недоступно')
        if task.status=='executing' and job['action']=='confirm':
            # The atomic reminder commit would have changed this state to done.
            self.service.store.set_general_task_status(task.id,'executing','confirming')
            task = self.service.store.get_general_task(task.id)
        if task.status!='confirming':
            if task.status=='done' and job['action']=='confirm' and task.payload.get('reminder_id'):
                self.store.finish(job['id'],'done',f"Напоминание #{task.payload['reminder_id']} сохранено на {task.payload.get('local_label') or task.payload['remind_at_utc']} ({self.service.owner_timezone}).")
                return
            if task.status=='cancelled' and job['action']=='cancel':
                self.store.finish(job['id'],'done','Напоминание отменено.')
                return
            self.store.finish(job['id'],'uncertain','Подтверждение уже обработано. Проверьте список напоминаний перед повтором.')
            return
        if job['action']=='refine':
            request = task.request_text + '\n\nУточнение владельца:\n' + job['refinement']
            result = await self.service._prepare_general_task(
                request,task_id=task.id,related_chat=chat,reminder_only=True,allowed_kinds={'reminder'},
                author={'created_by_name':user['name'],'web_user_id':user['id'],'web_job_id':job['id']},
            )
            if result is None:
                self.store.reminder_review(job['id'],task.id,'Уточните дату, время и текст напоминания.',False)
            else:
                self.review_reminder(job,result)
            return
        if job['action']=='confirm' and not self.valid_reminder(task):
            self.store.reminder_review(job['id'],task.id,'Время уже прошло или не задано. Уточните время и текст напоминания.',False)
            return
        result = await self.service.handle_general_task_action(task.id,job['action'],self.service.owner_chat_id)
        self.store.finish(job['id'],'done',result.text)

    async def notifications(self):
        from .push import PushService
        push = PushService(self.store,product=self.product)
        while True:
            try:
                await asyncio.to_thread(self.queue_notifications,push)
                await asyncio.to_thread(push.flush)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event=web_push_failed error_type=%s',type(exc).__name__)
            await asyncio.sleep(2)

    def queue_notifications(self,push):
        import time
        if self.product=='sally':
            from .sales_reminders import queue_notifications
            return queue_notifications(self,push)
        cutoff = time.time()-86400
        with self.store.connect() as db:
            jobs = db.execute("""SELECT id,user_id,context,state,revision,updated FROM jobs
                WHERE product='rick' AND state IN ('done','review','error','uncertain') AND updated>=?""",(cutoff,)).fetchall()
            owners = db.execute("SELECT id FROM users WHERE role='owner' AND disabled=0").fetchall()
        for job in jobs:
            if self.service.registry.get(int(job['context'])):
                push.enqueue(job['user_id'],f"answer:{job['id']}:{job['revision']}:{job['state']}",job['context'],
                             'answer',created_at=job['updated'])
        with self.service.store._connect() as db:
            reminders = db.execute("""SELECT id,related_chat_id,remind_at_utc FROM reminders
                WHERE owner_chat_id=? AND julianday(remind_at_utc)<=julianday('now')
                AND julianday(remind_at_utc)>=julianday('now','-1 day')""",(self.service.owner_chat_id,)).fetchall()
        for reminder in reminders:
            context = str(reminder['related_chat_id']) if reminder['related_chat_id'] else ''
            if context and not self.service.registry.get(int(context)):
                continue
            for owner in owners:
                push.enqueue(owner['id'],f"reminder:{reminder['id']}",context,'reminder',
                             created_at=datetime.fromisoformat(reminder['remind_at_utc']).timestamp())

    async def sales(self, job, user, payload):
        topic = int(job['context'])
        with self.service.store.connect() as db:
            deal = db.execute('SELECT title FROM deals WHERE chat_id=? AND topic_id=?', (self.settings.chat_id, topic)).fetchone()
        if not deal:
            raise WebError(404, 'Сделка недоступна')
        if self.service.store.awaiting_fact_review(self.settings.chat_id, topic):
            raise WebError(409, 'Сначала завершите проверку фактов в Telegram')
        from .sales_reminders import handle_reminder
        if await handle_reminder(self,job,user,payload):
            return
        from agentbridge.telegram.formatter import split_owner_message
        from agentbridge.sales.storage import should_respond
        attachments = self.store.media(user,'sally',job['context'],payload.get('attachments',[]))
        from agentbridge.documents import document_material
        documents = {}
        for item in attachments:
            if item['kind']=='document':
                with TemporaryDirectory(prefix='sally-document-') as folder:
                    documents[item['id']] = await asyncio.to_thread(document_material,Path(item['path']),Path(folder))
        transcripts = self.service.store.op_get('web-audio:'+str(job['id']),{})
        # Text is split by the same Telegram limits as all other owner delivery.
        parts = [(None,t) for t in split_owner_message(user['name']+' · через приложение\n'+payload['text'])]
        parts.extend((item,'') for item in attachments)
        delivered = []
        for part,(item,text) in enumerate(parts):
            saved = self.store.delivery(job['id'],part)
            if saved and saved['state']=='sending':
                self.store.finish(job['id'],'uncertain','Telegram мог принять сообщение. Проверьте тему перед повторной отправкой.')
                return
            if not saved or saved['state']!='sent':
                options = dict(chat_id=self.settings.chat_id, message_thread_id=topic or None)
                if payload.get('reply_to'):
                    from telegram import ReplyParameters
                    options['reply_parameters'] = ReplyParameters(message_id=payload['reply_to'])
                self.store.delivery(job['id'],part,state='sending')
                try:
                    if item:
                        path = Path(item['path'])
                        with path.open('rb') as stream:
                            # Documents preserve screenshot pixels and the browser audio container.
                            result = await self.app.bot.send_document(document=stream,filename=item['filename'],caption=user['name']+' · через приложение',**options)
                            file_id = result.document.file_id
                    else:
                        result = await self.app.bot.send_message(text=text,**options)
                        file_id = ''
                except Exception:
                    self.store.finish(job['id'],'uncertain','Доставка Telegram не подтверждена. Проверьте тему перед повтором; автоматического повтора нет.')
                    return
                saved = self.store.delivery(job['id'],part,state='sent',message_id=result.message_id,file_id=file_id)
            delivered.append(saved)
        # All sends were acknowledged. Ingest once with real Telegram IDs and internal authorship.
        # One text event plus attachment events enter the same normal topic batch.
        for part,((item,text),sent) in enumerate(zip(parts,delivered)):
            synthetic = -2_000_000_000_000 - job['id']*100 - part
            body = payload['text'] if part==0 else ''
            source, audio, image, media = 'web_text', None, None, None
            if part>0 and not item:
                body = ''  # Split transport parts are represented by the first full logical text.
                continue
            if item:
                media = dict(file_id=sent['file_id'],filename=item['filename'],mime=item['mime'],kind=item['kind'])
                if item['kind']=='image':
                    source = 'image_pending'
                    image = dict(**media,caption='',media_group_id='web:'+str(job['id']))
                elif item['kind']=='audio':
                    source = 'audio_pending'
                    audio = dict(**media, routing=dict(bot_username=self.app.bot.username or '',general=topic==0,
                        replied_to_bot=bool(payload.get('reply_to') and self.is_sally_reply(topic,payload['reply_to']))))
                    if item['id'] in transcripts:
                        body,source,audio = transcripts[item['id']],'audio_transcript',None
                else:
                    material = documents[item['id']]
                    if material['pages']:
                        source = 'image_pending'
                        image = dict(**media,caption='',media_group_id='web:'+str(job['id']))
                    else:
                        body = material['text']
                        source = 'text_document'
            response_text = payload['text']+'\n'+(transcripts.get(item['id'],'') if item else '')
            respond = should_respond(response_text,self.app.bot.username or '',general=topic==0,
                                     replied_to_bot=bool(payload.get('reply_to') and self.is_sally_reply(topic,payload['reply_to'])))
            self.service.store.ingest(update_id=synthetic,chat_id=self.settings.chat_id,topic_id=topic,title=deal['title'],
                message_id=sent['message_id'],author_id=-user['id'],author=user['name'],
                date=datetime.fromtimestamp(job['created'],timezone.utc).isoformat(),text=body,source=source,
                respond=respond or bool(audio),reply_to=payload.get('reply_to'),audio=audio,image=image,media=media)
            with self.service.store.connect() as db:
                event_id = db.execute('SELECT id FROM events WHERE update_id=?',(synthetic,)).fetchone()[0]
            if item:
                with self.store.connect() as db:
                    db.execute('UPDATE media SET file_id=?,event_id=? WHERE id=?',(sent['file_id'],event_id,item['id']))
        self.store.finish(job['id'],'waiting','Доставлено в Telegram. Материал сохранён в общей истории.')
        self.reconcile_sales()

    def is_sally_reply(self,topic,message_id):
        with self.service.store.connect() as db:
            return db.execute('SELECT 1 FROM outbox WHERE chat_id=? AND topic_id=? AND telegram_message_id=?',
                             (self.settings.chat_id,topic,message_id)).fetchone() is not None

    def reconcile_sales(self):
        with self.store.connect() as db:
            jobs = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE product='sally' AND task_id IS NULL AND state IN ('waiting','review')")]
        for job in jobs:
            with self.service.store.connect() as db:
                events = db.execute('SELECT id,processed FROM events WHERE update_id<=? AND update_id>?',
                    (-2_000_000_000_000-job['id']*100,-2_000_000_000_000-job['id']*100-100)).fetchall()
                if not events or any(e['processed'] in (-2,-1,0) for e in events):
                    if self.service.store.awaiting_fact_review(self.settings.chat_id,int(job['context'])):
                        self.store.finish(job['id'],'review','Материал сохранён. Ответ ожидает проверки фактов в Telegram.')
                    continue
                ids = {e['id'] for e in events}
                recs = db.execute('SELECT event_ids,payload FROM recommendations WHERE chat_id=? AND topic_id=? ORDER BY id DESC LIMIT 100',
                                 (self.settings.chat_id,int(job['context']))).fetchall()
                result = next((json.loads(r['payload'])['answer'] for r in recs if ids.intersection(json.loads(r['event_ids']))), '')
                self.store.finish(job['id'],'done',result or 'Сообщение доставлено и сохранено. Салли не отвечает на каждую реплику.')

    async def prepare_rick(self,job,user,payload,image_dir=None):
        from agentbridge.agents.base import MediaAttachment
        from agentbridge.documents import document_material
        from agentbridge.transcribe import transcribe_audio_file
        rows = self.store.media(user,'rick',job['context'],payload.get('attachments',[]))
        question, images = payload['text'], []
        for row in rows:
            path = Path(row['path'])
            if row['kind']=='image':
                images.append(MediaAttachment(str(path),'image',row['mime'],row['filename']))
            elif row['kind']=='audio':
                transcript = await transcribe_audio_file(path,api_key=self.settings.openai_api_key,model=self.settings.transcription_model)
                if not transcript or not transcript.strip():
                    raise WebError(422,'Речь не распознана. Исходная запись сохранена.')
                question += '\n[Расшифровка '+row['filename']+']\n'+transcript
            else:
                material = await asyncio.to_thread(document_material,path,image_dir/row['id'] if image_dir else None)
                question += '\n[Документ '+row['filename']+']\n'+material['text']
                for n, page in enumerate(material['pages'],1):
                    question += f'\n[Изображение страницы {n} документа {row["filename"]} приложено; не угадывай нечитаемый текст.]'
                    images.append(MediaAttachment(page,'image','image/png',f'{row["filename"]}, страница {n}'))
        if len(question)>150000:
            raise WebError(413,'Слишком много текста в одном запросе; разделите документы.')
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET prepared=? WHERE id=?', (question,job['id']))
        return question, tuple(images)

    async def refresh_media(self):
        if not hasattr(self,'app'):
            return
        from .media import cleanup
        from agentbridge.media import MAX_DOWNLOAD_BYTES
        import mimetypes
        import time
        if time.time()-getattr(self,'_last_media_refresh',0)<2:
            return
        self._last_media_refresh=time.time()
        # Metadata only: business history remains in the original stores.
        connect = self.service.store.connect if self.product=='sally' else self.service.store._connect
        with connect() as db:
            if self.product=='sally':
                records = [dict(r) for r in db.execute('''SELECT e.id,e.topic_id,m.metadata FROM event_media m
                    JOIN events e ON e.id=m.event_id WHERE e.chat_id=? AND e.processed>=0 AND e.processed!=2
                    ORDER BY e.id DESC LIMIT 200''',(self.settings.chat_id,))]
            else:
                records = [dict(id=r['id'],topic_id=r['chat_id'],metadata=json.dumps(dict(file_id=r['telegram_file_id'],
                    kind=r['media_kind'],mime=r['media_mime'],filename=r['media_filename']))) for r in db.execute('''SELECT * FROM telegram_messages
                    WHERE telegram_file_id!='' ORDER BY id DESC LIMIT 200''')]
        folder = self.store.path.parent/'media'
        folder.mkdir(exist_ok=True)
        with self.store.connect() as db:
            for row in records:
                if db.execute('SELECT 1 FROM media WHERE product=? AND event_id=?',(self.product,row['id'])).fetchone():
                    continue
                meta=json.loads(row['metadata'])
                ident=self.product+'-'+str(row['id'])
                suffix=Path(meta.get('filename','')).suffix.lower()
                if suffix not in {'.jpg','.jpeg','.png','.webp','.gif','.ogg','.mp3','.m4a','.wav','.webm','.flac','.pdf','.docx','.txt','.md'}:
                    suffix='.bin'
                kind=meta.get('kind','document')
                kind='image' if kind in ('photo','image') else 'audio' if kind in ('voice','audio') else 'document'
                db.execute('''INSERT OR IGNORE INTO media(id,user_id,product,context,filename,kind,mime,path,created,file_id,event_id)
                    VALUES (?,0,?,?,?,?,?,?,?,?,?)''',(ident,self.product,str(row['topic_id']),meta.get('filename') or 'file'+suffix,
                    kind,mimetypes.guess_type('a'+suffix)[0] or 'application/octet-stream',str(folder/(ident+suffix)),time.time(),meta['file_id'],row['id']))
            pending=[dict(r) for r in db.execute('SELECT * FROM media WHERE product=? AND requested=1 AND file_id!=? LIMIT 4',(self.product,''))]
        for row in pending:
            path=Path(row['path'])
            temp=path.with_suffix('.part')
            try:
                remote=await self.app.bot.get_file(row['file_id'])
                if not remote.file_size or remote.file_size>MAX_DOWNLOAD_BYTES:
                    continue
                await remote.download_to_drive(custom_path=temp)
                if temp.stat().st_size>MAX_DOWNLOAD_BYTES:
                    continue
                temp.replace(path)
                with self.store.connect() as db:
                    db.execute('UPDATE media SET requested=0,created=? WHERE id=?',(time.time(),row['id']))
            except Exception as exc:
                logger.warning('event=web_media_download_failed error_type=%s',type(exc).__name__)
            finally:
                temp.unlink(missing_ok=True)
        await asyncio.to_thread(cleanup,self.store)


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
