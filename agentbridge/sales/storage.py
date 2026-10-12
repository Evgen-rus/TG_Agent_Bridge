from __future__ import annotations

from contextlib import contextmanager
import json
import re
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from agentbridge.telegram.formatter import split_owner_message


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


class SalesStore:
    """Separate SQLite database; raw events survive every model failure."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deals (
                    chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, title TEXT NOT NULL,
                    retry_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,topic_id));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, update_id INTEGER NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL, author_id INTEGER NOT NULL, author TEXT NOT NULL,
                    date TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
                    reply_to INTEGER, processed INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(chat_id,message_id));
                CREATE INDEX IF NOT EXISTS events_topic ON events(chat_id,topic_id,id);
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                    field TEXT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL,
                    event_id INTEGER NOT NULL, quote TEXT NOT NULL,
                    superseded_by INTEGER);
                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                    event_ids TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                    delivery_key TEXT NOT NULL, part INTEGER NOT NULL, text TEXT NOT NULL,
                    telegram_message_id INTEGER,
                    UNIQUE(delivery_key,part));
                CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS document_chunks (
                    event_id INTEGER NOT NULL, chunk INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(event_id,chunk,fingerprint));
                CREATE TABLE IF NOT EXISTS turn_results (
                    request_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS restarts (
                    id INTEGER PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL,
                    chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS audio_jobs (
                    event_id INTEGER PRIMARY KEY, metadata TEXT NOT NULL,
                    caption TEXT NOT NULL, path TEXT NOT NULL DEFAULT '',
                    transcript TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    error_kind TEXT NOT NULL DEFAULT '');
            """)
            columns = {row[1] for row in db.execute('PRAGMA table_info(deals)')}
            for name, definition in (('thread_id', 'TEXT'), ('thread_version', 'TEXT'),
                                     ('thread_turns', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    db.execute(f'ALTER TABLE deals ADD COLUMN {name} {definition}')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, *, update_id, chat_id, topic_id, title, message_id,
               author_id, author, date, text, source="telegram_text", reply_to=None,
               respond=True, response_text=None, authorization_pending=False, audio=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if source not in {'text_document_pending', 'audio_pending'} and reply_to and db.execute("""SELECT 1 FROM outbox WHERE chat_id=? AND topic_id=?
                AND telegram_message_id=? AND delivery_key LIKE 'recommendation:%'""",
                (chat_id, topic_id, reply_to)).fetchone():
                source = 'manager_feedback'
            inserted = db.execute("""INSERT OR IGNORE INTO events
                (update_id,chat_id,topic_id,message_id,author_id,author,date,text,source,reply_to,processed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (update_id, chat_id, topic_id, message_id,
                author_id, author, date, text, source, reply_to,
                (-1 if respond else -2) if authorization_pending else int(not respond))).rowcount
            if inserted:
                if audio:
                    event_id = db.execute('SELECT id FROM events WHERE update_id=?', (update_id,)).fetchone()[0]
                    db.execute('INSERT INTO audio_jobs(event_id,metadata,caption) VALUES (?,?,?)', (event_id, encoded(audio), text))
                db.execute("INSERT OR IGNORE INTO deals(chat_id,topic_id,title) VALUES (?,?,?)",
                           (chat_id, topic_id, title))
                if response_text and not authorization_pending:
                    self._queue(db, chat_id, topic_id, f'help:{update_id}', response_text)
            return bool(inserted)

    def awaiting_authorization(self, chat_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT id,author_id,source FROM events
                WHERE chat_id=? AND processed<0 ORDER BY id LIMIT 50""", (chat_id,))]

    def authorize(self, row_id, allowed, help_text):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM events WHERE id=? AND processed<0', (row_id,)).fetchone()
            if not row:
                return
            # 2 means denied: retain the ingress record, exclude it from all model context.
            state = (0 if row['processed'] == -1 else 1) if allowed else 2
            db.execute('UPDATE events SET processed=? WHERE id=?', (state, row_id))
            if allowed and row['source'] == 'command':
                self._queue(db, row['chat_id'], row['topic_id'], f"help:{row['update_id']}", help_text)

    def pending_topics(self):
        with self.connect() as db:
            return [tuple(row) for row in db.execute("""SELECT d.chat_id,d.topic_id FROM deals d
                WHERE d.retry_at<=? AND EXISTS (SELECT 1 FROM events e WHERE
                e.chat_id=d.chat_id AND e.topic_id=d.topic_id AND e.processed=0 AND e.source NOT IN ('text_document_pending','audio_pending'))
                ORDER BY d.retry_at,d.topic_id""", (time.time(),))]

    def context(self, chat_id, topic_id):
        with self.connect() as db:
            scope = (chat_id, topic_id)
            title = db.execute("SELECT title FROM deals WHERE chat_id=? AND topic_id=?", scope).fetchone()[0]
            pending = [dict(row) for row in db.execute("""SELECT * FROM events WHERE
                chat_id=? AND topic_id=? AND processed=0 AND source NOT IN ('text_document_pending','audio_pending') ORDER BY id LIMIT 10""", scope)]
            facts = [dict(row) for row in db.execute("""SELECT * FROM facts WHERE
                chat_id=? AND topic_id=? AND superseded_by IS NULL ORDER BY id""", scope)]
            history = [dict(row) for row in db.execute("""SELECT * FROM events WHERE
                chat_id=? AND topic_id=? AND processed=1 AND source NOT IN ('text_document_pending','audio_pending') ORDER BY id DESC LIMIT 200""", scope)]
            previous = [dict(row) for row in db.execute("""SELECT * FROM recommendations WHERE
                chat_id=? AND topic_id=? ORDER BY id DESC LIMIT 5""", scope)]
        all_facts = facts
        terms = set(re.findall(r'\w{4,}', ' '.join(row['text'].lower() for row in pending)))
        terms -= {'клиент', 'ответить', 'нужно', 'сегодня', 'сделать', 'пожалуйста', 'сделка'}
        # Keep each card field represented, plus older facts relevant to the new event.
        chosen, per_field = {}, {}
        for fact in reversed(all_facts):
            if per_field.get(fact['field'], 0) < 4:
                chosen[fact['id']] = fact
                per_field[fact['field']] = per_field.get(fact['field'], 0) + 1
        ranked = sorted(all_facts, key=lambda f: len(terms & set(re.findall(r'\w{4,}', f['text'].lower()))), reverse=True)
        for fact in [f for f in ranked if terms & set(re.findall(r'\w{4,}', f['text'].lower()))][:12]:
            chosen[fact['id']] = fact
        for field in ('client', 'subject'):
            first = next((f for f in all_facts if f['field'] == field), None)
            if first:
                chosen[first['id']] = first
        facts = sorted(chosen.values(), key=lambda f: f['id'])
        # Recent sources plus older sources for the current ledger; no other topic is queried.
        source_ids = {fact['event_id'] for fact in facts}
        selected = history[:8] + [row for row in history[8:] if row['id'] in source_ids][:8]
        # Only historical excerpts are bounded; current documents are analyzed in full.
        for row in selected:
            row['truncated'] = len(row['text']) > 16000
            row['original_length'] = len(row['text'])
            row['text'] = row['text'][:16000]
        for row in pending:
            row.update(truncated=False, original_length=len(row['text']))
        return dict(chat_id=chat_id, topic_id=topic_id, title=title, facts=facts, omitted_facts=len(all_facts) - len(facts), history=list(reversed(selected)),
                    events=pending, previous_recommendations=[dict(id=r['id'], **json.loads(r['payload'])) for r in reversed(previous)])

    def complete(self, chat_id, topic_id, events, result):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [row['id'] for row in events]
            marks = ','.join('?' for _ in ids)
            pending = db.execute(f"SELECT count(*) FROM events WHERE id IN ({marks}) AND processed=0", ids).fetchone()[0]
            if pending != len(ids):
                return False
            for fact in result['facts']:
                fact_id = db.execute("""INSERT INTO facts(chat_id,topic_id,field,text,kind,event_id,quote)
                    VALUES (?,?,?,?,?,?,?)""", (chat_id, topic_id, fact['field'], fact['text'],
                    fact['kind'], fact['event_id'], fact['quote'])).lastrowid
                if fact['replaces']:
                    db.execute("UPDATE facts SET superseded_by=? WHERE id=? AND chat_id=? AND topic_id=?",
                               (fact_id, fact['replaces'], chat_id, topic_id))
            rec_id = db.execute("""INSERT INTO recommendations(chat_id,topic_id,event_ids,payload)
                VALUES (?,?,?,?)""", (chat_id, topic_id, encoded(ids), encoded(result))).lastrowid
            self._queue(db, chat_id, topic_id, f'recommendation:{rec_id}', result['answer'])
            db.execute(f"UPDATE events SET processed=1 WHERE id IN ({marks})", ids)
            db.execute("UPDATE deals SET retry_at=0 WHERE chat_id=? AND topic_id=?", (chat_id, topic_id))
            db.execute('UPDATE deals SET thread_turns=thread_turns+1 WHERE chat_id=? AND topic_id=?', (chat_id, topic_id))
            return True

    def failed(self, chat_id, topic_id, first_event_id):
        with self.connect() as db:
            db.execute("UPDATE deals SET retry_at=? WHERE chat_id=? AND topic_id=?",
                       (time.time() + 300, chat_id, topic_id))
            self._queue(db, chat_id, topic_id, f'failure:{first_event_id}',
                        'Сохранила сообщение. Сейчас анализ недоступен; повторю позже. История сделки сохранена.')

    @staticmethod
    def _queue(db, chat_id, topic_id, key, text):
        for part, chunk in enumerate(split_owner_message(text)):
            db.execute("INSERT OR IGNORE INTO outbox(chat_id,topic_id,delivery_key,part,text) VALUES (?,?,?,?,?)",
                       (chat_id, topic_id, key, part, chunk))

    def deliveries(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox WHERE telegram_message_id IS NULL ORDER BY id LIMIT 50")]

    def delivered(self, row_id, message_id):
        with self.connect() as db:
            db.execute("UPDATE outbox SET telegram_message_id=? WHERE id=?", (message_id, row_id))

    def thread(self, chat, topic):
        with self.connect() as db:
            row = db.execute('SELECT thread_id,thread_version,thread_turns FROM deals WHERE chat_id=? AND topic_id=?', (chat, topic)).fetchone()
            return dict(row) if row else {}

    def save_thread(self, chat, topic, thread_id, version, reason):
        with self.connect() as db:
            db.execute('UPDATE deals SET thread_id=?,thread_version=?,thread_turns=0 WHERE chat_id=? AND topic_id=?', (thread_id, version, chat, topic))
            db.execute('INSERT OR REPLACE INTO operations VALUES (?,?)', (f'thread-reason:{chat}:{topic}', encoded(reason)))

    def chunk_result(self, event_id, chunk, fingerprint, result=None):
        with self.connect() as db:
            if result is not None:
                db.execute('INSERT OR REPLACE INTO document_chunks VALUES (?,?,?,?)', (event_id, chunk, fingerprint, encoded(result)))
                return result
            row = db.execute('SELECT payload FROM document_chunks WHERE event_id=? AND chunk=? AND fingerprint=?', (event_id, chunk, fingerprint)).fetchone()
            return json.loads(row[0]) if row else None

    def turn_result(self, key, result=None):
        with self.connect() as db:
            if result is not None:
                db.execute('INSERT OR REPLACE INTO turn_results VALUES (?,?)', (key, encoded(result)))
                return result
            row = db.execute('SELECT payload FROM turn_results WHERE request_key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def op_get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM operations WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def op_set(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO operations VALUES (?,?)', (key, encoded(value)))

    def notice(self, chat, topic, key, text):
        with self.connect() as db:
            self._queue(db, chat, topic, key, text)

    def codex_failed(self, state, chat, topic, text=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO operations VALUES (?,?)', ('codex_failure', encoded(state)))
            if chat and text:
                self._queue(db, chat, topic, f"unavailable:{state['episode']}:{state['kind']}", text)

    def codex_recovered(self, chat, topic):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM operations WHERE key='codex_failure'").fetchone()
            old = json.loads(row[0]) if row else None
            if old:
                db.execute("UPDATE operations SET value='null' WHERE key='codex_failure'")
                db.execute('UPDATE deals SET retry_at=0')
                if chat:
                    self._queue(db, chat, topic, f"recovered:{old['episode']}", 'Codex снова доступен. Сали продолжает работу.')

    def begin_run(self, chat, topic):
        run_id = uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute("SELECT value FROM operations WHERE key='run'").fetchone()
            if old:
                previous = json.loads(old[0])
                if not previous['clean']:
                    self._queue(db, chat, topic, f"crash:{previous['id']}", 'Сали восстановилась после запуска без штатного завершения. Точная причина неизвестна; данные сделок сохранены.')
            db.execute('INSERT OR REPLACE INTO operations VALUES (?,?)', ('run', encoded(dict(id=run_id, clean=False))))
            db.execute("UPDATE restarts SET status='interrupted' WHERE status='claimed'")
            db.execute("UPDATE restarts SET status='completed' WHERE status='launched'")
        return run_id

    def end_run(self, run_id):
        with self.connect() as db:
            db.execute('UPDATE operations SET value=? WHERE key=? AND value=?', (encoded(dict(id=run_id, clean=True)), 'run', encoded(dict(id=run_id, clean=False))))

    def event_seen(self, update_id):
        with self.connect() as db:
            return bool(db.execute('SELECT 1 FROM events WHERE update_id=?', (update_id,)).fetchone())

    def awaiting_documents(self, chat):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events WHERE chat_id=? AND processed IN (0,1) AND source='text_document_pending' ORDER BY id LIMIT 10", (chat,))]

    def hydrate_document(self, row_id, text, source='text_document'):
        with self.connect() as db:
            db.execute("UPDATE events SET text=?,source=? WHERE id=? AND processed IN (0,1) AND source='text_document_pending'", (text, source, row_id))

    def awaiting_audio(self, chat):
        with self.connect() as db:
            rows = db.execute("""SELECT a.*,e.chat_id,e.topic_id,e.message_id FROM audio_jobs a
                JOIN events e ON e.id=a.event_id WHERE e.chat_id=? AND e.processed IN (0,1)
                AND a.state IN ('pending','downloaded') AND a.retry_at<=? ORDER BY a.event_id LIMIT 10""", (chat, time.time()))
            return [dict(dict(row), **json.loads(row['metadata'])) for row in rows]

    def audio_downloaded(self, row_id, path):
        with self.connect() as db:
            db.execute("UPDATE audio_jobs SET path=?,state='downloaded' WHERE event_id=? AND state IN ('pending','downloaded')", (str(path), row_id))

    def audio_transcribed(self, row_id, transcript):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM audio_jobs WHERE event_id=? AND state IN ('pending','downloaded')", (row_id,)).fetchone()
            if not row or not transcript.strip():
                return False
            text = ('Комментарий менеджера:\n' + row['caption'] + '\n\n' if row['caption'].strip() else '') + 'Распознанная речь:\n' + transcript
            db.execute("UPDATE audio_jobs SET transcript=?,state='transcribed',error_kind='',retry_at=0 WHERE event_id=?", (transcript, row_id))
            db.execute("UPDATE events SET text=?,source='audio_transcript' WHERE id=? AND source='audio_pending' AND processed IN (0,1)", (text, row_id))
            return True

    def audio_failed(self, row_id, reason, terminal=False):
        safe = {
            'audio_too_large': 'Аудиофайл слишком большой для текущего способа загрузки. Лимит — 20 МБ. Пришлите сжатую запись или готовую транскрибацию.',
            'audio_invalid': 'Не удалось прочитать формат аудио. Пришлите OGG/OPUS, MP3, M4A, WAV, WEBM или готовую транскрибацию.',
            'audio_empty': 'Аудиофайл пуст. Пришлите другую запись или готовую транскрибацию.',
            'audio_auth': 'Распознавание недоступно: проверьте настройку ключа OpenAI API Сали. Запись сохранена в очереди без анализа; после исправления пришлите её повторно.',
            'audio_quota': 'Распознавание недоступно: исчерпана API-квота OpenAI. Запись не анализировалась; после восстановления API пришлите её повторно.',
            'audio_request': 'API не смог принять эту запись или модель. Пришлите другую запись или готовую транскрибацию.',
            'audio_transcription_empty': 'Не удалось распознать речь. Содержание звонка не анализировалось; пришлите другую запись или готовую транскрибацию.',
        }
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT a.*,e.chat_id,e.topic_id FROM audio_jobs a JOIN events e ON e.id=a.event_id
                WHERE a.event_id=? AND a.state IN ('pending','downloaded')""", (row_id,)).fetchone()
            if not row:
                return
            attempts = row['attempts'] + 1
            terminal = terminal or attempts >= 5
            state = 'failed' if terminal else row['state']
            db.execute('UPDATE audio_jobs SET state=?,attempts=?,retry_at=?,error_kind=? WHERE event_id=?',
                       (state, attempts, time.time() + min(3600, 60 * 2 ** min(attempts-1, 6)), reason if reason in set(safe) | {'audio_download_failed','audio_transcription_failed'} else 'audio_transcription_failed', row_id))
            if terminal:
                db.execute('UPDATE events SET processed=3 WHERE id=? AND processed IN (0,1)', (row_id,))
            text = safe.get(reason, 'Не удалось разобрать аудио после ограниченных попыток. Содержание звонка не анализировалось. Пришлите запись повторно или готовую транскрибацию.' if terminal else 'Аудио сохранено в очереди. Распознавание временно недоступно; повторю позже. Коммерческий анализ ещё не выполнен.')
            self._queue(db, row['chat_id'], row['topic_id'], f"audio-error:{row_id}:{'terminal' if terminal else 'retry'}", text)

    def audio_cleanup(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT a.event_id,a.path FROM audio_jobs a JOIN events e ON e.id=a.event_id
                WHERE a.path!='' AND (a.state='failed' OR (a.state='transcribed' AND e.processed=1))""")]

    def audio_cleaned(self, row_id):
        with self.connect() as db:
            db.execute("UPDATE audio_jobs SET path='' WHERE event_id=?", (row_id,))

    def request_restart(self, chat, topic, user, update):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO restarts(update_id,chat_id,topic_id,user_id,created_at) VALUES (?,?,?,?,?)', (update, chat, topic, user, time.time()))
            row_id = db.execute('SELECT id FROM restarts WHERE update_id=?', (update,)).fetchone()[0]
            self._queue(db, chat, topic, f'restart-confirm:{row_id}', 'Перезапустить Сали? Сделки сохранены. Подтвердите кнопкой; Рик продолжит работать.')
            return row_id

    def get_restart(self, row_id):
        with self.connect() as db:
            row = db.execute("""SELECT r.*, o.telegram_message_id AS confirmation_message_id FROM restarts r
                LEFT JOIN outbox o ON o.delivery_key='restart-confirm:' || r.id AND o.part=0
                WHERE r.id=?""", (row_id,)).fetchone()
            return dict(row) if row else None

    def claim_restart(self, row_id, user):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM restarts WHERE status IN ('claimed','launched')").fetchone():
                return False
            return bool(db.execute("UPDATE restarts SET status='claimed' WHERE id=? AND user_id=? AND status='pending' AND created_at>?", (row_id, user, time.time()-600)).rowcount)

    def finish_restart(self, row_id, status):
        with self.connect() as db:
            db.execute("UPDATE restarts SET status=? WHERE id=? AND status IN ('claimed','launched')", (status, row_id))
