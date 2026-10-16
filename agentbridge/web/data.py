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

    def contexts(self, user, product):
        if not WebStore.allowed(user, product):
            raise WebError(403, 'Нет доступа к приложению')
        if product == 'rick':
            rows = [dict(id=str(c.telegram_chat_id), title=c.name) for c in self.registry.all_chats()]
        else:
            with reader(self.sales_path) as db:
                rows = [dict(id=str(r['topic_id']), title=r['title']) for r in db.execute(
                    'SELECT topic_id,title FROM deals WHERE chat_id=? AND topic_id>0 ORDER BY topic_id DESC', (self.sales_chat,))]
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
            return dict(**item, events=events, recommendations=recommendations,
                        facts=json.loads(state[0]) if state else {}, commitments=reminders, reviews=[],
                        telegram_url=self.telegram_url(self.owner_chat) if user['role']=='owner' else '')
        with reader(self.sales_path) as db:
            scope = (self.sales_chat, int(context))
            events = [dict(r) for r in db.execute('''SELECT id,author,text,date,source,processed FROM events
                WHERE chat_id=? AND topic_id=? AND processed NOT IN (-1,-2,2)
                AND source NOT IN ('audio_pending','image_pending','text_document_pending') ORDER BY id DESC LIMIT 100''', scope)][::-1]
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
        return dict(**item,events=events,recommendations=recommendations,facts=facts,commitments=commitments,reviews=reviews,
                    telegram_url=self.telegram_url(self.sales_chat,context))

    @staticmethod
    def telegram_url(chat, message=''):
        value=str(chat or '')
        return f'https://t.me/c/{value[4:]}/{message}' if value.startswith('-100') else ''
