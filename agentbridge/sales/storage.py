from __future__ import annotations

from contextlib import contextmanager
import json
import re
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from agentbridge.telegram.formatter import split_owner_message
from .communication import validate_profile_updates
from agentbridge.storage.revisions import allow_message_edits


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


SALI_NAME = r'(?:сал{1,2}и|sal{1,2}[iy])'


def should_respond(text, bot_username, addressed_elsewhere=False):
    mentions = re.findall(r'@([A-Za-z0-9_]+)', text)
    mentions_sali = any(name.lower() == bot_username.lower() for name in mentions)
    explicit = mentions_sali or bool(re.search(rf'(?i)\b{SALI_NAME}\b', text))
    quiet = bool(re.fullmatch(r'(?i)\s*(спасибо|ок|окей|понял[аи]?|принято|\+)\s*[.! ]*', text))
    return explicit or not (quiet or addressed_elsewhere or (mentions and not mentions_sali))


class SalesStore:
    """Separate SQLite database; raw events survive every model failure."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deals (
                    chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, title TEXT NOT NULL,
                    retry_at REAL NOT NULL DEFAULT 0, batch_cutoff_id INTEGER, PRIMARY KEY(chat_id,topic_id));
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
                CREATE TABLE IF NOT EXISTS image_jobs (
                    event_id INTEGER PRIMARY KEY, metadata TEXT NOT NULL,
                    path TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, error_kind TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS communication_profile_revisions (
                    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                    contact_id TEXT NOT NULL, payload TEXT NOT NULL, event_ids TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS communication_profiles (
                    chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, contact_id TEXT NOT NULL,
                    revision_id INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(chat_id,topic_id,contact_id));
                CREATE TABLE IF NOT EXISTS review_episodes (
                    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                    event_ids TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(chat_id,topic_id,event_ids));
                CREATE UNIQUE INDEX IF NOT EXISTS review_episode_pending ON review_episodes(chat_id,topic_id)
                    WHERE state='pending';
                CREATE TABLE IF NOT EXISTS fact_reviews (
                    id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, old_fact_id INTEGER NOT NULL,
                    patch TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    decided_by INTEGER, decided_at REAL);
            """)
            columns = {row[1] for row in db.execute('PRAGMA table_info(deals)')}
            for name, definition in (('thread_id', 'TEXT'), ('thread_version', 'TEXT'),
                                     ('thread_turns', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('batch_cutoff_id', 'INTEGER')):
                if name not in columns:
                    db.execute(f'ALTER TABLE deals ADD COLUMN {name} {definition}')
            event_columns = {row[1] for row in db.execute('PRAGMA table_info(events)')}
            if 'received_at' not in event_columns:
                # Legacy pending events are catch-up; receipt time is no longer recoverable.
                db.execute('ALTER TABLE events ADD COLUMN received_at REAL NOT NULL DEFAULT 0')
            allow_message_edits(db, "events")
            from .commitments import DDL
            db.executescript(DDL)
            columns = {row[1] for row in db.execute('PRAGMA table_info(commitments)')}
            for name, definition in (('remind_at', 'REAL'), ('reminder_event_id', 'INTEGER'),
                                     ('reminder_sent_at', 'REAL')):
                if name not in columns:
                    db.execute(f'ALTER TABLE commitments ADD COLUMN {name} {definition}')

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
               respond=True, response_text=None, authorization_pending=False, audio=None, image=None, is_edit=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO deals(chat_id,topic_id,title) VALUES (?,?,?)",
                       (chat_id, topic_id, title))
            self._freeze_batch_if_due(db, chat_id, topic_id, time.time())
            if source not in {'text_document_pending', 'audio_pending', 'image_pending'} and reply_to and db.execute("""SELECT 1 FROM outbox WHERE chat_id=? AND topic_id=?
                AND telegram_message_id=? AND delivery_key LIKE 'recommendation:%'""",
                (chat_id, topic_id, reply_to)).fetchone():
                source = 'manager_feedback'
            inserted = db.execute("""INSERT OR IGNORE INTO events
                (update_id,chat_id,topic_id,message_id,author_id,author,date,text,source,reply_to,processed,received_at,edit_update_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (update_id, chat_id, topic_id, message_id,
                author_id, author, date, text, source, reply_to,
                (-1 if respond else -2) if authorization_pending else int(not respond), time.time(), update_id if is_edit else 0)).rowcount
            if inserted:
                event_id = db.execute('SELECT id FROM events WHERE update_id=?', (update_id,)).fetchone()[0]
                if audio:
                    db.execute('INSERT INTO audio_jobs(event_id,metadata,caption) VALUES (?,?,?)', (event_id, encoded(audio), text))
                if image:
                    db.execute('INSERT INTO image_jobs(event_id,metadata) VALUES (?,?)', (event_id, encoded(image)))
                if response_text and not authorization_pending:
                    self._queue(db, chat_id, topic_id, f'help:{update_id}', response_text)
            return bool(inserted)

    @staticmethod
    def _freeze_batch_if_due(db, chat_id, topic_id, now):
        deal = db.execute('SELECT batch_cutoff_id FROM deals WHERE chat_id=? AND topic_id=?',
                          (chat_id, topic_id)).fetchone()
        if not deal or deal['batch_cutoff_id'] is not None:
            return
        first = db.execute("""SELECT id,received_at FROM events WHERE chat_id=? AND topic_id=?
            AND processed IN (-1,0) ORDER BY id LIMIT 1""", (chat_id, topic_id)).fetchone()
        if not first:
            return
        started = last = first['received_at']
        cutoff = first['id']
        for row in db.execute("""SELECT id,received_at FROM events WHERE chat_id=? AND topic_id=?
            AND id>? AND processed!=2 ORDER BY id""", (chat_id, topic_id, cutoff)):
            if row['received_at'] >= min(last + 15, started + 60):
                break
            last, cutoff = row['received_at'], row['id']
        if now >= min(last + 15, started + 60):
            db.execute('UPDATE deals SET batch_cutoff_id=? WHERE chat_id=? AND topic_id=?',
                       (cutoff, chat_id, topic_id))

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

    def pending_topics(self, *, batched=False):
        with self.connect() as db:
            if batched:
                db.execute("BEGIN IMMEDIATE")
                now = time.time()
                ready = []
                for deal in db.execute("SELECT chat_id,topic_id,retry_at FROM deals ORDER BY retry_at,topic_id").fetchall():
                    chat, topic = deal['chat_id'], deal['topic_id']
                    self._freeze_batch_if_due(db, chat, topic, now)
                    if deal['retry_at'] > now:
                        continue
                    cutoff = db.execute("SELECT batch_cutoff_id FROM deals WHERE chat_id=? AND topic_id=?",
                                        (chat, topic)).fetchone()[0]
                    if cutoff is not None and not db.execute("""SELECT 1 FROM events WHERE chat_id=? AND topic_id=?
                        AND id<=? AND processed IN (-2,-1,0)""",
                        (chat, topic, cutoff)).fetchone():
                        db.execute("UPDATE deals SET batch_cutoff_id=NULL WHERE chat_id=? AND topic_id=?",
                                   (chat, topic))
                        cutoff = None
                    if cutoff is None:
                        self._freeze_batch_if_due(db, chat, topic, now)
                        cutoff = db.execute("SELECT batch_cutoff_id FROM deals WHERE chat_id=? AND topic_id=?",
                                            (chat, topic)).fetchone()[0]
                    if cutoff is None:
                        continue
                    if db.execute("""SELECT 1 FROM events WHERE chat_id=? AND topic_id=? AND id<=?
                        AND (processed<0 OR (processed IN (0,1) AND source IN
                        ('text_document_pending','audio_pending','image_pending'))) LIMIT 1""", (chat, topic, cutoff)).fetchone():
                        continue
                    if db.execute("""SELECT 1 FROM events WHERE chat_id=? AND topic_id=? AND id<=?
                        AND processed=0 AND source NOT IN ('text_document_pending','audio_pending','image_pending') LIMIT 1""",
                        (chat, topic, cutoff)).fetchone():
                        if not db.execute("""SELECT 1 FROM review_episodes WHERE chat_id=? AND topic_id=?
                            AND state='pending'""", (chat, topic)).fetchone():
                            ready.append((chat, topic))
                return ready
            return [tuple(row) for row in db.execute("""SELECT d.chat_id,d.topic_id FROM deals d
                WHERE d.retry_at<=? AND EXISTS (SELECT 1 FROM events e WHERE
                e.chat_id=d.chat_id AND e.topic_id=d.topic_id AND e.processed=0 AND e.source NOT IN ('text_document_pending','audio_pending','image_pending'))
                AND NOT EXISTS (SELECT 1 FROM review_episodes r WHERE r.chat_id=d.chat_id
                    AND r.topic_id=d.topic_id AND r.state='pending')
                ORDER BY d.retry_at,d.topic_id""", (time.time(),))]

    def context(self, chat_id, topic_id, *, batch=False):
        with self.connect() as db:
            if batch:
                db.execute("BEGIN IMMEDIATE")
                self._freeze_batch_if_due(db, chat_id, topic_id, time.time())
                deal = db.execute("SELECT batch_cutoff_id,retry_at FROM deals WHERE chat_id=? AND topic_id=?",
                                  (chat_id, topic_id)).fetchone()
                cutoff = deal['batch_cutoff_id'] if deal else None
                if cutoff is None or deal['retry_at'] > time.time() or db.execute("""SELECT 1 FROM events WHERE chat_id=? AND topic_id=? AND id<=?
                    AND (processed<0 OR (processed IN (0,1) AND source IN
                    ('text_document_pending','audio_pending','image_pending'))) LIMIT 1""",
                    (chat_id, topic_id, cutoff)).fetchone():
                    return dict(events=[], history=[], facts=[], title='', chat_id=chat_id,
                                topic_id=topic_id, omitted_facts=0, communication_profiles=[],
                                previous_recommendations=[])
            scope = (chat_id, topic_id)
            title = db.execute("SELECT title FROM deals WHERE chat_id=? AND topic_id=?", scope).fetchone()[0]
            pending = [dict(row) for row in db.execute("""SELECT * FROM events WHERE
                chat_id=? AND topic_id=? AND processed=0 AND source NOT IN ('text_document_pending','audio_pending','image_pending')
                AND (?=0 OR id<=?) ORDER BY id""" + ("" if batch else " LIMIT 10"),
                (*scope, cutoff if batch else 0, cutoff if batch else 0))]
            facts = [dict(row) for row in db.execute("""SELECT * FROM facts WHERE
                chat_id=? AND topic_id=? AND superseded_by IS NULL ORDER BY id""", scope)]
            history = [dict(row) for row in db.execute("""SELECT * FROM events WHERE
            chat_id=? AND topic_id=? AND processed IN (1,3) AND source NOT IN ('text_document_pending','audio_pending','image_pending')
            AND (?=0 OR id<=?) ORDER BY id DESC LIMIT 200""",
                (*scope, cutoff if batch else 0, cutoff if batch else 0))]
            previous = [dict(row) for row in db.execute("""SELECT * FROM recommendations WHERE
                chat_id=? AND topic_id=? ORDER BY id DESC LIMIT 5""", scope)]
            profiles = [json.loads(row[0]) for row in db.execute("""SELECT payload FROM communication_profiles
                WHERE chat_id=? AND topic_id=? ORDER BY revision_id DESC LIMIT 8""", scope)]
            self._source_metadata(db, pending + history)
            for profile in profiles:
                invalidated = profile.get('invalidated_event_ids', [])
                profile['omitted_invalidations'] = max(0, len(invalidated) - 50)
                profile['invalidated_event_ids'] = invalidated[-50:]
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
        source_ids.update(item['event_id'] for profile in profiles for item in profile['evidence'])
        selected = history[:8] + [row for row in history[8:] if row['id'] in source_ids][:8]
        # Only historical excerpts are bounded; current documents are analyzed in full.
        for row in selected:
            row['truncated'] = len(row['text']) > 16000
            row['original_length'] = len(row['text'])
            row['text'] = row['text'][:16000]
        for row in pending:
            row.update(truncated=False, original_length=len(row['text']))
        return dict(chat_id=chat_id, topic_id=topic_id, title=title, facts=facts, omitted_facts=len(all_facts) - len(facts), history=list(reversed(selected)),
                    events=pending, communication_profiles=profiles,
                    previous_recommendations=[dict(id=r['id'], **json.loads(r['payload'])) for r in reversed(previous)])

    def complete(self, chat_id, topic_id, events, result):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [row['id'] for row in events]
            marks = ','.join('?' for _ in ids)
            pending = db.execute(f"SELECT count(*) FROM events WHERE id IN ({marks}) AND processed=0 AND chat_id=? AND topic_id=?", (*ids, chat_id, topic_id)).fetchone()[0]
            if pending != len(ids):
                return False
            if db.execute("SELECT 1 FROM review_episodes WHERE chat_id=? AND topic_id=? AND state='pending'", (chat_id, topic_id)).fetchone():
                return False
            if result.get('fact_reviews'):
                return self._stage_reviews(db, chat_id, topic_id, events, result)
            self._apply_result(db, chat_id, topic_id, events, result)
            return True

    @classmethod
    def _apply_result(cls, db, chat, topic, events, result):
        ids = [row['id'] for row in events]
        cls._complete_profiles(db, chat, topic, events, result.get('communication_profiles', []))
        for fact in result['facts']:
            fact_id = db.execute('''INSERT INTO facts(chat_id,topic_id,field,text,kind,event_id,quote)
                VALUES (?,?,?,?,?,?,?)''', (chat, topic, fact['field'], fact['text'],
                fact['kind'], fact['event_id'], fact['quote'])).lastrowid
            if fact['replaces']:
                db.execute('UPDATE facts SET superseded_by=? WHERE id=? AND chat_id=? AND topic_id=?',
                           (fact_id, fact['replaces'], chat, topic))
        rec_id = db.execute('''INSERT INTO recommendations(chat_id,topic_id,event_ids,payload)
            VALUES (?,?,?,?)''', (chat, topic, encoded(ids), encoded(result))).lastrowid
        if not result.get('_reminder_handled'):
            cls._queue(db, chat, topic, f'recommendation:{rec_id}', result['answer'])
        if result.get('_commitment_check'):
            from .commitments import attach_context_check
            attach_context_check(db, chat, topic, rec_id, result['_commitment_check'])
        marks = ','.join('?' for _ in ids)
        db.execute(f'UPDATE events SET processed=1 WHERE id IN ({marks})', ids)
        db.execute('UPDATE deals SET retry_at=0,thread_turns=thread_turns+1 WHERE chat_id=? AND topic_id=?', (chat, topic))
        db.execute('''UPDATE deals SET batch_cutoff_id=NULL WHERE chat_id=? AND topic_id=?
            AND NOT EXISTS (SELECT 1 FROM events WHERE chat_id=? AND topic_id=?
                AND id<=deals.batch_cutoff_id AND processed IN (-2,-1,0))''', (chat, topic, chat, topic))

    @classmethod
    def _stage_reviews(cls, db, chat, topic, events, result):
        from .service import validate_result
        ids = [e['id'] for e in events]
        marks = ','.join('?' for _ in ids)
        sources = [dict(row) for row in db.execute(f'''SELECT * FROM events
            WHERE chat_id=? AND topic_id=? AND processed=0 AND id IN ({marks})''', (chat, topic, *ids))]
        cls._source_metadata(db, sources)
        old = [dict(row) for row in db.execute('SELECT * FROM facts WHERE chat_id=? AND topic_id=? AND superseded_by IS NULL', (chat, topic))]
        # Re-check every fact against durable sources before creating owner-visible cards.
        patches = validate_result(dict(answer='review', next_action='', goal='', basis=[],
            facts=result['facts'], fact_reviews=result['fact_reviews']),
            dict(events=sources, history=[], facts=old), max_facts=2000, allow_fact_reviews=True)
        if not patches.get('fact_reviews'):
            raise ValueError('Invalid fact review')
        existing = db.execute("SELECT id FROM review_episodes WHERE chat_id=? AND topic_id=? AND (event_ids=? OR state='pending')",
                              (chat, topic, encoded(ids))).fetchone()
        if existing:
            return False
        result = {**result, 'facts': patches['facts'], 'fact_reviews': patches['fact_reviews']}
        episode = db.execute('INSERT INTO review_episodes(chat_id,topic_id,event_ids,payload) VALUES (?,?,?,?)',
                             (chat, topic, encoded(ids), encoded(result))).lastrowid
        old = {row['id']: row for row in old}
        for patch in patches['fact_reviews']:
            review = db.execute('''INSERT INTO fact_reviews(episode_id,chat_id,topic_id,old_fact_id,patch)
                VALUES (?,?,?,?,?)''', (episode, chat, topic, patch['replaces'], encoded(patch))).lastrowid
            cls._queue(db, chat, topic, f'fact-review:{review}',
                'Подтвердите изменение памяти сделки.\n\n'
                f"Сейчас: {old[patch['replaces']]['text']}\n\n"
                f"Предложение модели: {patch['text']}\n\nОснование: {patch['quote']}\n\n"
                'Без подтверждения прежняя запись сохранится. Ответ и обновление памяти ждут решения по всем изменениям этого запроса.')
        return True

    def awaiting_fact_review(self, chat, topic):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM review_episodes WHERE chat_id=? AND topic_id=? AND state='pending'", (chat, topic)).fetchone())

    def decide_fact_review(self, review_id, chat_id, topic_id, message_id, user_id, accept):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT r.* FROM fact_reviews r JOIN outbox o
                ON o.delivery_key='fact-review:'||r.id WHERE r.id=? AND r.chat_id=?
                AND r.topic_id=? AND o.chat_id=? AND o.topic_id=? AND o.telegram_message_id=?''',
                (review_id, chat_id, topic_id, chat_id, topic_id, message_id)).fetchone()
            if not row:
                return 'denied'
            episode = db.execute('SELECT * FROM review_episodes WHERE id=?', (row['episode_id'],)).fetchone()
            if row['state'] != 'pending' or episode['state'] != 'pending':
                return 'already'
            all_reviews = list(db.execute('SELECT * FROM fact_reviews WHERE episode_id=?', (episode['id'],)))
            stale = any(not db.execute('''SELECT 1 FROM facts WHERE id=? AND chat_id=?
                AND topic_id=? AND superseded_by IS NULL''', (r['old_fact_id'], chat_id, topic_id)).fetchone() for r in all_reviews)
            status = 'stale' if stale else ('accepted' if accept else 'rejected')
            db.execute('UPDATE fact_reviews SET state=?,decided_by=?,decided_at=? WHERE id=?',
                       (status, user_id, time.time(), review_id))
            if status == 'accepted' and db.execute("SELECT 1 FROM fact_reviews WHERE episode_id=? AND state='pending'", (episode['id'],)).fetchone():
                return status
            ids = json.loads(episode['event_ids'])
            marks = ','.join('?' for _ in ids)
            events = [dict(r) for r in db.execute(f'''SELECT * FROM events WHERE chat_id=? AND topic_id=?
                AND processed=0 AND id IN ({marks})''', (chat_id, topic_id, *ids))]
            if len(events) != len(ids):
                status = 'stale'
                db.execute("UPDATE fact_reviews SET state='stale' WHERE id=?", (review_id,))
            result = json.loads(episode['payload'])
            reviews = result.pop('fact_reviews')
            if status == 'accepted':
                if any(not db.execute('''SELECT 1 FROM facts WHERE id=? AND chat_id=? AND topic_id=?
                        AND superseded_by IS NULL AND field=?''', (f['replaces'], chat_id, topic_id, f['field'])).fetchone()
                        for f in result['facts'] + reviews if f['replaces']):
                    status = 'stale'
                    db.execute("UPDATE fact_reviews SET state='stale' WHERE id=?", (review_id,))
            if status == 'accepted':
                result['facts'] += reviews
            else:
                result.update(answer=('Запись памяти уже изменилась; подтверждение устарело.' if status == 'stale' else
                    'Изменение отклонено. Прежние записи памяти сохранены.') +
                    ' Ответ модели, опиравшийся на спорное изменение, не отправлен. Уточните данные или повторите вопрос.',
                    facts=[], communication_profiles=[], goal='', next_action='', basis=[], client_draft=False)
                db.execute("UPDATE fact_reviews SET state='cancelled' WHERE episode_id=? AND state='pending'", (episode['id'],))
            db.execute('UPDATE review_episodes SET state=? WHERE id=?', (status, episode['id']))
            if events:
                self._apply_result(db, chat_id, topic_id, events, result)
            else:
                self._queue(db, chat_id, topic_id, f'review-stale:{episode["id"]}', result['answer'])
            return status

    def invalid_result(self, chat, topic, events):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for event in events:
                db.execute('UPDATE events SET processed=3 WHERE id=? AND chat_id=? AND topic_id=? AND processed=0', (event['id'], chat, topic))
            self._queue(db, chat, topic, f'validation:{events[0]["id"]}',
                'Ответ модели не прошёл проверку. Исходные материалы сохранены; автоматические повторы этого запроса остановлены. Уточните данные или повторите вопрос.')

    @staticmethod
    def _source_metadata(db, rows):
        for row in rows:
            if row['source'] == 'image_transcript':
                job = db.execute('SELECT payload,metadata FROM image_jobs WHERE event_id=?', (row['id'],)).fetchone()
                row['image_evidence'] = json.loads(job['payload']) if job and job['payload'] else []
                row['image_caption'] = json.loads(job['metadata']).get('caption', '') if job else ''
            if row['source'] != 'audio_transcript':
                continue
            job = db.execute('SELECT caption FROM audio_jobs WHERE event_id=?', (row['id'],)).fetchone()
            caption = job['caption'] if job else ''
            prefix = ('Комментарий менеджера:\n' + caption + '\n\n' if caption.strip() else '') + 'Распознанная речь:\n'
            row['speech_offset'] = len(prefix) if job and row['text'].startswith(prefix) else len(row['text'])

    @staticmethod
    def _complete_profiles(db, chat_id, topic_id, events, updates):
        if not updates:
            return
        scope = (chat_id, topic_id)
        previous = {row['contact_id']: json.loads(row['payload']) for row in db.execute(
            'SELECT contact_id,payload FROM communication_profiles WHERE chat_id=? AND topic_id=?', scope)}
        # Re-check against persisted sources inside the same transaction as facts and delivery.
        source_ids = {item['event_id'] for profile in updates for item in profile.get('evidence', [])}
        history = []
        if source_ids:
            marks = ','.join('?' for _ in source_ids)
            history = [dict(row) for row in db.execute(f'''SELECT * FROM events WHERE chat_id=?
                AND topic_id=? AND processed IN (0,1) AND id IN ({marks})''', (*scope, *source_ids))]
        ids = [row['id'] for row in events]
        marks = ','.join('?' for _ in ids)
        current = [dict(row) for row in db.execute(f'''SELECT * FROM events WHERE chat_id=?
            AND topic_id=? AND processed=0 AND id IN ({marks})''', (*scope, *ids))]
        SalesStore._source_metadata(db, history + current)
        validated = validate_profile_updates(updates, dict(events=current, history=history,
                                                          communication_profiles=list(previous.values())))
        for profile in validated:
            contact = profile['contact_id']
            invalidated = set(previous.get(contact, {}).get('invalidated_event_ids', []))
            invalidated.update(profile['correction']['invalidated_event_ids'])
            profile['invalidated_event_ids'] = sorted(invalidated)
            payload = encoded(profile)
            if previous.get(contact) == profile:
                continue
            revision = db.execute('''INSERT INTO communication_profile_revisions
                (chat_id,topic_id,contact_id,payload,event_ids,created_at) VALUES (?,?,?,?,?,?)''',
                (*scope, contact, payload, encoded(ids), time.time())).lastrowid
            db.execute('''INSERT INTO communication_profiles VALUES (?,?,?,?,?)
                ON CONFLICT(chat_id,topic_id,contact_id) DO UPDATE SET
                revision_id=excluded.revision_id,payload=excluded.payload''', (*scope, contact, revision, payload))

    def profile_history(self, chat_id, topic_id, contact_id):
        with self.connect() as db:
            return [dict(row, payload=json.loads(row['payload'])) for row in db.execute('''SELECT *
                FROM communication_profile_revisions WHERE chat_id=? AND topic_id=? AND contact_id=?
                ORDER BY id''', (chat_id, topic_id, contact_id))]

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
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE outbox SET telegram_message_id=? WHERE id=?", (message_id, row_id))
            from .commitments import acknowledge
            acknowledge(db, row_id, time.time())

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

    def awaiting_images(self, chat):
        with self.connect() as db:
            rows = db.execute("""SELECT i.*,e.chat_id,e.topic_id,e.message_id,e.text AS caption
                FROM image_jobs i JOIN events e ON e.id=i.event_id
                WHERE e.chat_id=? AND e.processed IN (0,1) AND i.state='pending'
                AND i.retry_at<=? ORDER BY i.event_id LIMIT 10""", (chat, time.time()))
            return [dict(dict(row), **json.loads(row['metadata'])) for row in rows]

    def image_downloaded(self, event_id, path):
        with self.connect() as db:
            db.execute("UPDATE image_jobs SET path=? WHERE event_id=? AND state='pending'", (path, event_id))

    def image_read(self, event_id, evidence):
        from .images import validate_image
        evidence = validate_image({'evidence': evidence})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT e.* FROM events e JOIN image_jobs i ON i.event_id=e.id
                WHERE e.id=? AND e.source='image_pending' AND e.processed IN (0,1)
                AND i.state='pending'""", (event_id,)).fetchone()
            if not row:
                return False
            labels = {'readable_text': 'Читаемый текст', 'visual_observation': 'Визуальное наблюдение (не подтверждение)',
                      'uncertain': 'Неясно / не подтверждено'}
            text = ('Комментарий менеджера:\n' + row['text'] + '\n\n' if row['text'] else '')
            text += f"Изображение из сообщения {row['message_id']} (event_id={event_id}):\n"
            text += '\n\n'.join(labels[item['kind']] + ':\n' + item['text'] for item in evidence)
            db.execute("UPDATE image_jobs SET payload=?,state='read',error_kind='',retry_at=0 WHERE event_id=?", (encoded(evidence), event_id))
            db.execute("UPDATE events SET text=?,source='image_transcript' WHERE id=?", (text, event_id))
            return True

    def image_failed(self, event_id, reason, terminal=False):
        safe = {
            'image_too_large': 'Изображение не прочитано: лимит загрузки Telegram — 20 МБ. Пришлите уменьшенную копию или разделите длинный скриншот, сохранив читаемость.',
            'image_invalid': 'Изображение не прочитано: файл повреждён, формат не поддерживается или Codex не смог вернуть надёжное извлечение. Пришлите JPEG/PNG/WEBP/GIF или текст.',
            'image_download_failed': 'Изображение сохранено в очереди, но загрузка временно недоступна. Повторю позже; содержимое ещё не анализировалось.',
        }
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT i.*,e.chat_id,e.topic_id,e.text FROM image_jobs i
                JOIN events e ON e.id=i.event_id WHERE i.event_id=? AND i.state='pending'""", (event_id,)).fetchone()
            if not row:
                return
            attempts = row['attempts'] + 1
            terminal = terminal or attempts >= 5
            reason = reason if reason in safe else 'image_invalid'
            db.execute('UPDATE image_jobs SET state=?,attempts=?,retry_at=?,error_kind=? WHERE event_id=?',
                ('failed' if terminal else 'pending', attempts, time.time() + min(3600, 60 * 2 ** min(attempts-1, 6)), reason, event_id))
            text = (safe[reason] if not terminal or reason != 'image_download_failed' else
                'Не удалось загрузить изображение после пяти попыток. Содержимое не анализировалось; пришлите его повторно.')
            if terminal:
                # Keep captions/questions available, but never claim the missing image was read.
                db.execute("UPDATE events SET source='unsupported_attachment',text=? WHERE id=? AND source='image_pending'",
                    ('[Изображение не прочитано. Содержимое неизвестно.]\n' + row['text'], event_id))
            self._queue(db, row['chat_id'], row['topic_id'], f"image-error:{event_id}:{'terminal' if terminal else 'retry'}", text)

    def image_cleanup(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT i.event_id,i.path FROM image_jobs i
                JOIN events e ON e.id=i.event_id WHERE i.path!='' AND
                (i.state='failed' OR (i.state='read' AND e.processed IN (1,2,3)))""")]

    def image_cleaned(self, event_id):
        with self.connect() as db:
            db.execute("UPDATE image_jobs SET path='' WHERE event_id=?", (event_id,))

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
            routing = json.loads(row['metadata']).get('routing')
            if routing is not None:
                respond = should_respond((row['caption'] + '\n' + transcript).strip(), **routing)
                db.execute("UPDATE events SET processed=? WHERE id=? AND source='audio_pending' AND processed IN (0,1)", (int(not respond), row_id))
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
