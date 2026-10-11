from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

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
            """)

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
               respond=True, response_text=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if reply_to and db.execute("""SELECT 1 FROM outbox WHERE chat_id=? AND topic_id=?
                AND telegram_message_id=? AND delivery_key LIKE 'recommendation:%'""",
                (chat_id, topic_id, reply_to)).fetchone():
                source = 'manager_feedback'
            inserted = db.execute("""INSERT OR IGNORE INTO events
                (update_id,chat_id,topic_id,message_id,author_id,author,date,text,source,reply_to,processed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (update_id, chat_id, topic_id, message_id,
                author_id, author, date, text, source, reply_to, int(not respond))).rowcount
            if inserted:
                db.execute("INSERT OR IGNORE INTO deals(chat_id,topic_id,title) VALUES (?,?,?)",
                           (chat_id, topic_id, title))
                if response_text:
                    self._queue(db, chat_id, topic_id, f'help:{update_id}', response_text)
            return bool(inserted)

    def pending_topics(self):
        with self.connect() as db:
            return [tuple(row) for row in db.execute("""SELECT d.chat_id,d.topic_id FROM deals d
                WHERE d.retry_at<=? AND EXISTS (SELECT 1 FROM events e WHERE
                e.chat_id=d.chat_id AND e.topic_id=d.topic_id AND e.processed=0)
                ORDER BY d.retry_at,d.topic_id""", (time.time(),))]

    def context(self, chat_id, topic_id):
        with self.connect() as db:
            scope = (chat_id, topic_id)
            title = db.execute("SELECT title FROM deals WHERE chat_id=? AND topic_id=?", scope).fetchone()[0]
            pending = [dict(row) for row in db.execute("""SELECT * FROM events WHERE
                chat_id=? AND topic_id=? AND processed=0 ORDER BY id LIMIT 10""", scope)]
            facts = [dict(row) for row in db.execute("""SELECT * FROM facts WHERE
                chat_id=? AND topic_id=? AND superseded_by IS NULL ORDER BY id""", scope)]
            history = [dict(row) for row in db.execute("""SELECT * FROM events WHERE
                chat_id=? AND topic_id=? AND processed=1 ORDER BY id DESC LIMIT 200""", scope)]
            previous = [dict(row) for row in db.execute("""SELECT * FROM recommendations WHERE
                chat_id=? AND topic_id=? ORDER BY id DESC LIMIT 5""", scope)]
        # Recent sources plus older sources for the current ledger; no other topic is queried.
        source_ids = {fact['event_id'] for fact in facts}
        selected = history[:8] + [row for row in history[8:] if row['id'] in source_ids][:8]
        # shortcut: bounded excerpts for long transcripts; original events remain in SQLite.
        for row in pending + selected:
            row['truncated'] = len(row['text']) > 16000
            row['original_length'] = len(row['text'])
            row['text'] = row['text'][:16000]
        return dict(title=title, facts=facts, history=list(reversed(selected)),
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
