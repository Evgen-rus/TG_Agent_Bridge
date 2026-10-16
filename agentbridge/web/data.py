from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

from .store import WebError, WebStore


@contextmanager
def reader(path):
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


class WebData:
    def __init__(self, store, registry, rick_path, sales_path, sales_chat, owner_chat=None):
        self.store, self.registry = store, registry
        self.rick_path, self.sales_path, self.sales_chat = rick_path, sales_path, sales_chat
        self.owner_chat = owner_chat

    def reminder(self,user,ident,product='rick'):
        if product=='sally':
            if not WebStore.allowed(user,product):
                raise WebError(403,'Напоминание недоступно')
            with reader(self.sales_path) as db:
                row = db.execute('SELECT c.*,d.title FROM commitments c JOIN deals d ON d.chat_id=c.chat_id AND d.topic_id=c.topic_id WHERE c.chat_id=? AND c.id=? AND c.remind_at IS NOT NULL',(self.sales_chat,ident)).fetchone()
            if not row:
                raise WebError(404,'Напоминание недоступно')
            from datetime import datetime,timezone
            return dict(id=row['id'],action=row['action'],remind_at_utc=datetime.fromtimestamp(row['remind_at'],timezone.utc).isoformat(),sent_at=row['reminder_sent_at'],related_chat_name=row['title'])
        if user['role']!='owner' or not WebStore.allowed(user,'rick'):
            raise WebError(403,'Напоминание доступно владельцу')
        with reader(self.rick_path) as db:
            row = db.execute('''SELECT id,text AS action,remind_at_utc,sent_at,related_chat_name
                FROM reminders WHERE id=? AND owner_chat_id=?''',(ident,self.owner_chat)).fetchone()
        if not row:
            raise WebError(404,'Напоминание недоступно')
        return dict(row)

    def notifications(self,user,product,after):
        import time
        now = time.time()
        after = max(float(after),now-86400)
        if not WebStore.allowed(user,product):
            raise WebError(403,'Нет доступа')
        notifications = []
        if product=='sally':
            with reader(self.sales_path) as db:
                enabled = db.execute("SELECT value FROM operations WHERE key='commitments_enabled'").fetchone()
                if enabled and not json.loads(enabled[0]):
                    return dict(notifications=[],cursor=now)
                rows = db.execute("SELECT id,topic_id,remind_at FROM commitments WHERE chat_id=? AND state='active' AND quiet=0 AND remind_at>? AND remind_at<=?",(self.sales_chat,after,now)).fetchall()
            for row in rows:
                notifications.append(dict(id=f"sally-reminder:{row['id']}:{row['remind_at']}",context=str(row['topic_id']),reminder_id=row['id'],url=f"/sally/?context={row['topic_id']}&reminder={row['id']}"))
        elif user['role']=='owner':
            with reader(self.rick_path) as db:
                rows = db.execute("SELECT id,related_chat_id,remind_at_utc FROM reminders WHERE owner_chat_id=? AND julianday(remind_at_utc)>julianday(?,'unixepoch') AND julianday(remind_at_utc)<=julianday(?,'unixepoch')",(self.owner_chat,after,now)).fetchall()
            for row in rows:
                context = str(row['related_chat_id']) if row['related_chat_id'] else ''
                notifications.append(dict(id=f"rick-reminder:{row['id']}",context=context,reminder_id=row['id'],url=f"/rick/?context={context}&reminder={row['id']}"))
        return dict(notifications=notifications,cursor=now)

    def contexts(self, user, product):
        if not WebStore.allowed(user, product):
            raise WebError(403, 'Нет доступа к приложению')
        if product == 'rick':
            rows = [dict(id=str(c.telegram_chat_id), title=c.name) for c in self.registry.all_chats()]
        else:
            with reader(self.sales_path) as db:
                rows = [dict(id=str(r['topic_id']), title=r['title']) for r in db.execute(
                    'SELECT topic_id,title FROM deals WHERE chat_id=? AND topic_id>=0 ORDER BY topic_id DESC', (self.sales_chat,))]
        rows = [r for r in rows if WebStore.allowed(user, product, r['id'])]
        with reader(self.rick_path if product=='rick' else self.sales_path) as db:
            for row in rows:
                if product=='rick':
                    latest = db.execute('''SELECT situation AS summary,suggested_reply AS draft,id
                        FROM recommendations WHERE telegram_chat_id=? ORDER BY id DESC LIMIT 1''',(int(row['id']),)).fetchone()
                    if latest:
                        row['latest']=dict(latest)
                else:
                    latest=db.execute('SELECT id,payload FROM recommendations WHERE chat_id=? AND topic_id=? ORDER BY id DESC LIMIT 1',(self.sales_chat,int(row['id']))).fetchone()
                    if latest:
                        payload=json.loads(latest['payload'])
                        row['latest']=dict(id=latest['id'],summary=payload.get('next_action') or 'Ответ Салли',draft=payload.get('answer',''))
        rows.sort(key=lambda r:r.get('latest',{}).get('id',0),reverse=True)
        if product=='sally':
            rows.sort(key=lambda r:r['id']!='0')
        return rows

    def context(self, user, product, context):
        row = next((c for c in self.contexts(user, product) if c['id'] == context), None)
        if not row:
            raise WebError(404, 'Контекст недоступен')
        return row

    def detail(self, user, product, context):
        item = self.context(user, product, context)
        if product == 'rick':
            with reader(self.rick_path) as db:
                events = [dict(r) for r in db.execute('''SELECT id,sender_name AS author,text,telegram_date AS date
                    FROM telegram_messages WHERE chat_id=? ORDER BY id DESC LIMIT 100''', (int(context),))][::-1]
                recommendations = [dict(r) for r in db.execute('''SELECT id,situation AS summary,suggested_reply AS draft,
                    original_message AS source,unknowns,owner_question,owner_message_id
                    FROM recommendations WHERE telegram_chat_id=? ORDER BY id DESC LIMIT 20''', (int(context),))]
                state = db.execute('SELECT state_json FROM chat_states WHERE telegram_chat_id=?', (int(context),)).fetchone()
                reminders = []
                if user['role'] == 'owner':
                    reminders = [dict(r) for r in db.execute('''SELECT id,text AS action,remind_at_utc,sent_at FROM reminders
                        WHERE related_chat_id=? ORDER BY id DESC LIMIT 30''', (int(context),))]
            with self.store.connect() as db:
                media=[dict(r) for r in db.execute("SELECT id,event_id,filename,kind,mime FROM media WHERE product='rick' AND context=? AND user_id=0",(context,))]
            for event in events:
                event['attachments']=[r for r in media if r['event_id']==event['id']]
            return dict(**item, events=events, recommendations=recommendations,
                        facts=json.loads(state[0]) if state else {}, commitments=reminders, reviews=[],
                        telegram_url=self.telegram_url(self.owner_chat) if user['role']=='owner' else '')
        with reader(self.sales_path) as db:
            scope = (self.sales_chat, int(context))
            events = [dict(r) for r in db.execute('''SELECT id,author,text,date,source,processed,message_id,reply_to,edit_update_id FROM events
                WHERE chat_id=? AND topic_id=? AND processed NOT IN (-1,-2,2)
                ORDER BY id DESC LIMIT 100''', scope)][::-1]
            for event in events:
                event['key']='event:'+str(event['id'])
                if event['source']=='text_document_pending':
                    meta=json.loads(event['text'])
                    event['text']=(meta.get('caption') or '')+'\n[Документ обрабатывается]'
            # Responses are the original durable Telegram outbox, not per-user Web results.
            messages=list(events)
            for row in db.execute('SELECT * FROM outbox WHERE chat_id=? AND topic_id=? ORDER BY id DESC LIMIT 100',scope):
                if not row['delivery_key'].startswith(('recommendation:','reminder:')):
                    continue
                messages.append(dict(key='outbox:'+str(row['id']),id=row['id'],author='Салли',text=row['text'],
                    date='',message_id=row['telegram_message_id'],processed=1 if row['telegram_message_id'] else 0,source='sally',
                    delivery_uncertain=bool(not row['telegram_message_id'] and db.execute('SELECT 1 FROM operations WHERE key=?',('web-response:'+str(row['id']),)).fetchone())))
            messages.sort(key=lambda e:(e['message_id'] if e['message_id'] and e['message_id']>0 else 10**15,e.get('edit_update_id',0)))
            with self.store.connect() as transport:
                media=[dict(r) for r in transport.execute('SELECT id,event_id,filename,kind,mime FROM media WHERE product=? AND context=? AND event_id IS NOT NULL',('sally',context))]
            for event in events:
                event['attachments']=[r for r in media if r['event_id']==event['id']]
            # Historical image/audio jobs also carry usable Telegram IDs before Web was enabled.
            for event in events:
                if event.get('source') in ('audio_pending','image_pending') and not event['text']:
                    event['text']='[Вложение обрабатывается]'
            recommendations = []
            for r in db.execute('SELECT id,payload FROM recommendations WHERE chat_id=? AND topic_id=? ORDER BY id DESC LIMIT 20', scope):
                payload = json.loads(r['payload'])
                recommendations.append(dict(id=r['id'], summary=payload.get('next_action') or 'Ответ Салли',
                    draft=payload.get('answer',''), source='', unknowns='', owner_question=''))
            facts = [dict(r) for r in db.execute('''SELECT id,field,text,kind,quote FROM facts
                WHERE chat_id=? AND topic_id=? AND superseded_by IS NULL ORDER BY id''', scope)]
            commitments = [dict(r) for r in db.execute('''SELECT id,action,state,quote,due_date,remind_at,reminder_sent_at,revision
                FROM commitments WHERE chat_id=? AND topic_id=? ORDER BY updated_at DESC LIMIT 50''', scope)]
            reviews = [dict(r) for r in db.execute('''SELECT r.id,r.old_fact_id,r.patch,o.telegram_message_id
                FROM fact_reviews r LEFT JOIN outbox o ON o.delivery_key='fact-review:'||r.id
                WHERE r.chat_id=? AND r.topic_id=? AND r.state='pending' ''', scope)]
        return dict(**item,events=events,messages=messages,recommendations=recommendations,facts=facts,commitments=commitments,reviews=reviews,
                    telegram_url=self.telegram_url(self.sales_chat,context))

    def reply_target(self,context,message_id):
        with reader(self.sales_path) as db:
            scope=(self.sales_chat,int(context),message_id)
            if not (db.execute('SELECT 1 FROM events WHERE chat_id=? AND topic_id=? AND message_id=? AND processed>=0 AND processed!=2',scope).fetchone()
                    or db.execute('SELECT 1 FROM outbox WHERE chat_id=? AND topic_id=? AND telegram_message_id=?',scope).fetchone()):
                raise WebError(404,'Сообщение для ответа недоступно')

    @staticmethod
    def telegram_url(chat, message=''):
        value=str(chat or '')
        return f'https://t.me/c/{value[4:]}/{message}' if value.startswith('-100') else ''
