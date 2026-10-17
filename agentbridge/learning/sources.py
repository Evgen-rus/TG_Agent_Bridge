"""Read saved source material without changing operational message state."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import sqlite3

CHUNK_CHARS = 18000


def timestamp(value):
    if isinstance(value, (float, int)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _utc(value):
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return str(value)


def telegram_url(chat_id, message_id):
    if not chat_id or not message_id or message_id <= 0:
        return ''
    if str(chat_id).startswith('-100'):
        return f'https://t.me/c/{str(chat_id)[4:]}/{message_id}'
    if chat_id > 0:
        return f'tg://openmessage?user_id={chat_id}&message_id={message_id}'
    return ''


def _source(key, text, context, author, date, url=''):
    text = str(text).strip()
    if not text or text.startswith('/') or re.fullmatch(r'(?i)(привет|здравствуйте|доброе утро|добрый день|добрый вечер|спасибо|ок|окей|понял[аи]?|принято)[.!\s]*',text):
        return []
    version = hashlib.sha256(text.encode()).hexdigest()
    return [dict(source_id=key + (f':part:{offset // CHUNK_CHARS}' if len(text) > CHUNK_CHARS else ''),
                 version=version, text=text[offset:offset + CHUNK_CHARS], context=str(context),
                 author=str(author), date=_utc(date), url=url, scope='chat' if str(context) else 'global')
            for offset in range(0, len(text), CHUNK_CHARS)]


def _rows(db, table):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    return db.execute(f'SELECT * FROM {table}').fetchall()


def read_sources(path: Path, product: str, cutoff: float, web_path: Path | None = None):
    sources = []
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if product == 'sally':
            events = {r['id']: dict(r) for r in _rows(db, 'events')}
            for r in events.values():
                if r['source'] in {'audio_pending', 'image_pending', 'unsupported_attachment'}:
                    continue
                parts = _source(f"events:{r['id']}", r['text'], r['topic_id'], r['author'], r['date'],
                                telegram_url(r['chat_id'], r['message_id']))
                for part in parts:
                    part['received_at'] = r.get('received_at', 0)
                sources += parts
            for r in _rows(db, 'recommendations'):
                ids = json.loads(r['event_ids'])
                context_events = [events[i] for i in ids if i in events]
                if not context_events:
                    continue
                payload = json.loads(r['payload'])
                text = str(payload.get('answer') or '')
                sources += _source(f"recommendations:{r['id']}", text, r['topic_id'], 'Салли',
                                   context_events[-1]['date'], telegram_url(context_events[-1]['chat_id'], context_events[-1]['message_id']))
        else:
            recommendations = {r['id']: dict(r) for r in _rows(db, 'recommendations')}
            for r in _rows(db, 'telegram_messages'):
                if r['role'] in {'client', 'internal', 'owner'}:
                    context = '' if r['role'] == 'owner' else str(r['chat_id'])
                    sources += _source(f"telegram_messages:{r['id']}", r['text'], context,
                                       r['sender_name'], r['created_at'], telegram_url(r['chat_id'], r['message_id']))
            for r in _rows(db, 'internal_context_messages'):
                sources += _source(f"internal_context_messages:{r['id']}", r['message_text'],
                                   r['telegram_chat_id'], r['sender_name'], r['created_at'])
            for r in recommendations.values():
                sources += _source(f"recommendations:{r['id']}", r['situation'] + '\n' + r['suggested_reply'],
                                   r['telegram_chat_id'], 'Рик', r['created_at'], telegram_url(r['owner_chat_id'], r['owner_message_id']))
            for r in _rows(db, 'learning_drafts'):
                rec = recommendations.get(r['recommendation_id'])
                context = str(rec['telegram_chat_id']) if rec and r['scope'] == 'client' else ''
                text = 'Исправление: ' + r['feedback'] + '\nПонимание: ' + r['understanding']
                sources += _source(f"learning_drafts:{r['id']}", text, context, r['author_name'], r['updated_at'],
                                   telegram_url(rec['owner_chat_id'], rec['owner_message_id']) if rec else '')
            for table, field, author in [('owner_query_prompts', 'question', 'Владелец'),
                                         ('owner_query_deliveries', 'text', 'Рик'),
                                         ('owner_general_tasks', 'request_text', 'Владелец')]:
                for r in _rows(db, table):
                    sources += _source(f"{table}:{r['id']}", r[field], '', author, r['created_at'])
    if web_path and web_path.is_file():
        with sqlite3.connect(web_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            for r in db.execute('SELECT jobs.*, users.name AS author FROM jobs JOIN users ON users.id=jobs.user_id WHERE product=?', (product,)):
                if r['state'] != 'done':
                    continue
                question = r['prepared'] or json.loads(r['payload']).get('text', '')
                sources += _source(f"web_jobs:{r['id']}", question + '\nОтвет: ' + r['result'],
                                   r['context'], r['author'], r['created'])
    # The persisted initial cutoff never advances, so catch-up and edits remain eligible.
    return sorted((s for s in sources if max(timestamp(s['date']), float(s.get('received_at', 0))) >= cutoff),
                  key=lambda s: (timestamp(s['date']), s['source_id']))
