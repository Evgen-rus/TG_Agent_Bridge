from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def tokens(text):
    # Russian inflections share prefixes; synonyms remain explicit rule metadata.
    return {word[:6] for word in re.findall(r'[\w]+', text.casefold()) if len(word) > 2}


class LearningStore:
    def __init__(self, path):
        self.path = Path(path)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS wiki_rules (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL, situation TEXT NOT NULL,
                    behavior TEXT NOT NULL, rationale TEXT NOT NULL, keywords TEXT NOT NULL,
                    scope TEXT NOT NULL, context TEXT NOT NULL, sources TEXT NOT NULL,
                    status TEXT NOT NULL, version INTEGER NOT NULL, previous_id INTEGER,
                    author TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT UNIQUE NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS wiki_sources (
                    source_id TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL,
                    processed_at REAL NOT NULL, PRIMARY KEY(source_id,version));
                CREATE TABLE IF NOT EXISTS wiki_state (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wiki_outbox (
                    id INTEGER PRIMARY KEY, candidate_id INTEGER NOT NULL, version INTEGER NOT NULL,
                    message_id INTEGER, UNIQUE(candidate_id,version));
                CREATE TABLE IF NOT EXISTS wiki_prompts (
                    chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,candidate_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,PRIMARY KEY(chat_id,message_id));
                CREATE TABLE IF NOT EXISTS wiki_threads (
                    thread_id TEXT PRIMARY KEY, rules TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wiki_legacy (
                    legacy_id INTEGER PRIMARY KEY,rule_id INTEGER NOT NULL);
            ''')
        self.import_legacy()

    def import_legacy(self):
        """Copy legacy rules once, retaining the original rows and their evidence."""
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_rules'").fetchone():
                return
            db.execute('BEGIN IMMEDIATE')
            db.execute('''UPDATE wiki_rules SET status='disabled',updated_at=? WHERE status='active'
                AND id IN (SELECT l.rule_id FROM wiki_legacy l JOIN learning_rules r ON r.id=l.legacy_id
                           WHERE r.status!='active')''',(time.time(),))
            for row in db.execute('SELECT * FROM learning_rules WHERE id NOT IN (SELECT legacy_id FROM wiki_legacy)').fetchall():
                source = db.execute('SELECT * FROM learning_drafts WHERE id=?',(row['source_draft_id'],)).fetchone()
                evidence = dict(source_id=f"legacy-rule:{row['id']}",version='1',text=source['feedback'] if source else row['rule_text'],
                    context=str(row['telegram_chat_id'] or ''),author=row['author_name'],date=row['created_at'],url='')
                cursor = db.execute('''INSERT INTO wiki_rules
                    (title,situation,behavior,rationale,keywords,scope,context,sources,status,version,
                    author,fingerprint,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (row['rule_text'][:120],'При подходящей ситуации в обсуждении',row['rule_text'],
                     source['understanding'] if source else 'Ранее подтверждено командой',encoded([]),
                     'global' if row['scope']=='global' else 'chat',str(row['telegram_chat_id'] or ''),
                     encoded([evidence]),'active' if row['status']=='active' else 'disabled',1,row['author_name'],
                     f"legacy-rule:{row['id']}",time.time()))
                db.execute('INSERT INTO wiki_legacy VALUES (?,?)',(row['id'],cursor.lastrowid))

    def legacy_texts(self):
        self.import_legacy()
        with self.connect() as db:
            return {r[0] for r in db.execute('SELECT r.rule_text FROM learning_rules r JOIN wiki_legacy l ON l.legacy_id=r.id')}

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _row(row):
        result = dict(row)
        for key in ('sources', 'keywords'):
            result[key] = json.loads(result[key])
        return result

    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT * FROM wiki_rules WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            return self._row(row)

    def snapshot(self):
        self.import_legacy()
        with self.connect() as db:
            rows = [self._row(row) for row in db.execute('SELECT * FROM wiki_rules ORDER BY updated_at DESC,id DESC')]
        return dict(candidates=[r for r in rows if r['status']=='pending'],
                    rules=[r for r in rows if r['status']=='active'],
                    history=[r for r in rows if r['status']!='pending'])

    def initial_cutoff(self, now):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO wiki_state VALUES (?,?)', ('initial_cutoff',str(now-14*86400)))
            return float(db.execute('SELECT value FROM wiki_state WHERE key=?', ('initial_cutoff',)).fetchone()[0])

    def sources_processed(self, keys):
        with self.connect() as db:
            saved = {(r[0],r[1]) for r in db.execute('SELECT source_id,version FROM wiki_sources')}
        return set(keys) & saved

    @staticmethod
    def _validate(candidate):
        for key in ('title','situation','behavior','rationale'):
            value = candidate.get(key)
            if not isinstance(value,str) or not value.strip() or len(value)>1500:
                raise ValueError('Invalid lesson text')
        if candidate.get('scope') not in ('global','chat'):
            raise ValueError('Invalid lesson scope')
        if candidate['scope']=='chat' and not candidate.get('context'):
            raise ValueError('Chat lesson requires context')
        words = candidate.get('keywords',[])
        if not isinstance(words,list) or len(words)>30 or any(not isinstance(w,str) or len(w)>100 for w in words):
            raise ValueError('Invalid lesson keywords')

    def commit_batch(self, sources, candidates):
        source_map = {s['source_id']:s for s in sources}
        prepared = []
        for candidate in candidates:
            self._validate(candidate)
            ids = candidate.get('source_ids',[])
            if not ids or any(i not in source_map for i in ids):
                raise ValueError('Lesson has no verified sources')
            evidence = [source_map[i] for i in ids]
            context = candidate.get('context','') if candidate['scope']=='chat' else ''
            if candidate['scope']=='chat' and any(str(s.get('context',''))!=context for s in evidence):
                raise ValueError('Cross-context lesson')
            prepared.append((candidate,evidence,context))
        added = []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for candidate,evidence,context in prepared:
                fingerprint = hashlib.sha256(encoded([candidate['scope'],context,
                    ' '.join(candidate['behavior'].casefold().split())]).encode()).hexdigest()
                previous_id = candidate.get('previous_id')
                version = 1
                if previous_id:
                    previous = db.execute('SELECT * FROM wiki_rules WHERE id=? AND status=?', (previous_id,'active')).fetchone()
                    if previous is None or previous['scope']!=candidate['scope'] or previous['context']!=context:
                        raise ValueError('Invalid superseded lesson')
                    version = previous['version']+1
                row = db.execute('''INSERT OR IGNORE INTO wiki_rules
                    (title,situation,behavior,rationale,keywords,scope,context,sources,status,version,
                     previous_id,author,fingerprint,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (candidate['title'],candidate['situation'],candidate['behavior'],candidate['rationale'],
                     encoded(candidate.get('keywords',[])),candidate['scope'],context,encoded(evidence),
                     'pending',version,previous_id,'learning',fingerprint,time.time()))
                if row.rowcount:
                    added.append(row.lastrowid)
                    db.execute('INSERT INTO wiki_outbox(candidate_id,version) VALUES (?,?)',(row.lastrowid,version))
            for source in sources:
                db.execute('INSERT OR IGNORE INTO wiki_sources VALUES (?,?,?,?)',
                    (source['source_id'],source['version'],encoded(source),time.time()))
        return added

    def decide(self, ident, action, author, comment='', expected_version=None, changes=None):
        if action not in ('accept','reject','refine','disable') or not isinstance(comment,str) or len(comment)>4000:
            raise ValueError('Invalid lesson action')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM wiki_rules WHERE id=?',(ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            rule = self._row(row)
            if expected_version is not None and rule['version']!=expected_version:
                raise ValueError('Lesson version changed')
            target = {'accept':'active','reject':'rejected','disable':'disabled'}.get(action)
            if target and rule['status']==target:
                return rule
            if action=='refine':
                edits = changes or {}
                if set(edits)-{'title','situation','behavior','rationale'}:
                    raise ValueError('Invalid lesson fields')
                revised = {**rule, **edits}
                self._validate(revised)
                fingerprint = hashlib.sha256(encoded([ident,rule['version'],edits,comment]).encode()).hexdigest()
                current = db.execute('SELECT id FROM wiki_rules WHERE fingerprint=?',(fingerprint,)).fetchone()
                if current:
                    return self._row(db.execute('SELECT * FROM wiki_rules WHERE id=?',(current[0],)).fetchone())
                if rule['status'] not in ('pending','active'):
                    raise ValueError('Lesson is no longer editable')
                # Keep every reviewed text immutable; a correction always requires acceptance.
                db.execute("UPDATE wiki_rules SET status='revised',updated_at=? WHERE id=? AND status='pending'",(time.time(),ident))
                previous_id = ident if rule['status']=='active' else rule['previous_id']
                cursor = db.execute('''INSERT INTO wiki_rules
                    (title,situation,behavior,rationale,keywords,scope,context,sources,status,version,
                     previous_id,author,comment,fingerprint,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (revised['title'],revised['situation'],revised['behavior'],revised['rationale'],
                     encoded(rule['keywords']),rule['scope'],rule['context'],encoded(rule['sources']),
                     'pending',rule['version']+1,previous_id,str(author),comment,fingerprint,time.time()))
                ident = cursor.lastrowid
                db.execute('INSERT INTO wiki_outbox(candidate_id,version) VALUES (?,?)',(ident,rule['version']+1))
            else:
                required = 'active' if action=='disable' else 'pending'
                if rule['status']!=required:
                    raise ValueError('Lesson already reviewed')
                if action=='accept' and rule['previous_id']:
                    updated = db.execute("UPDATE wiki_rules SET status='superseded',updated_at=? WHERE id=? AND status='active'",(time.time(),rule['previous_id']))
                    if not updated.rowcount:
                        raise ValueError('Previous lesson changed; review again')
                db.execute('UPDATE wiki_rules SET status=?,author=?,comment=?,updated_at=? WHERE id=?',
                    (target,str(author),comment,time.time(),ident))
            return self._row(db.execute('SELECT * FROM wiki_rules WHERE id=?',(ident,)).fetchone())

    def pending_notifications(self):
        with self.connect() as db:
            rows = db.execute('''SELECT o.* FROM wiki_outbox o JOIN wiki_rules r ON r.id=o.candidate_id
                WHERE o.message_id IS NULL AND r.status='pending' ORDER BY o.id''').fetchall()
            return [{**dict(r),'payload':self._row(db.execute('SELECT * FROM wiki_rules WHERE id=?',(r['candidate_id'],)).fetchone())} for r in rows]

    def notification_delivered(self, ident, message_id):
        with self.connect() as db:
            db.execute('UPDATE wiki_outbox SET message_id=? WHERE id=? AND message_id IS NULL',(message_id,ident))

    def save_prompt(self, chat_id, message_id, candidate_id, version):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO wiki_prompts VALUES (?,?,?,?)',(chat_id,message_id,candidate_id,version))

    def prompt(self, chat_id, message_id):
        with self.connect() as db:
            row = db.execute('SELECT candidate_id,version FROM wiki_prompts WHERE chat_id=? AND message_id=?',(chat_id,message_id)).fetchone()
            return dict(row) if row else None

    def search(self, query, context):
        self.import_legacy()
        query_words = tokens(query)
        if not query_words:
            return []
        with self.connect() as db:
            rows = [self._row(r) for r in db.execute("SELECT * FROM wiki_rules WHERE status='active' AND (scope='global' OR context=?)",(str(context),))]
        ranked = []
        for row in rows:
            match = len(query_words & tokens(' '.join([row['title'],row['situation'],row['behavior'],*row['keywords']])))
            if match:
                ranked.append((match,row['id'],row))
        result, size = [], 0
        for _,_,row in sorted(ranked, key=lambda x:(-x[0],-x[1])):
            text = self.rule_text(row)
            if size+len(text)<=5900:
                result.append(row)
                size += len(text)
            if len(result)==3:
                break
        return result

    @staticmethod
    def rule_text(row):
        return f"[{row['id']} v{row['version']}] {row['title']}\nКогда: {row['situation']}\nДействие: {row['behavior']}\nПочему: {row['rationale']}"

    def context_for_thread(self, query, context, thread_id=None):
        """Returns text, must_reset; only acknowledge after a successful model turn."""
        selected = self.search(query,context)
        sent = []
        with self.connect() as db:
            row = db.execute('SELECT rules FROM wiki_threads WHERE thread_id=?',(thread_id,)).fetchone()
            if row:
                sent = json.loads(row[0])
            active = {r[0] for r in db.execute("SELECT id FROM wiki_rules WHERE status='active'")}
        reset = any(ident not in active for ident in sent)
        extra = [r for r in selected if reset or r['id'] not in sent]
        text = '\n\n'.join(self.rule_text(r) for r in extra)
        return ('Подтверждённые правила Wiki (применять только в указанной ситуации):\n'+text if text else ''), reset, [r['id'] for r in selected]

    def remember_thread(self, thread_id, rule_ids):
        with self.connect() as db:
            old = db.execute('SELECT rules FROM wiki_threads WHERE thread_id=?',(thread_id,)).fetchone()
            ids = list(dict.fromkeys((json.loads(old[0]) if old else [])+rule_ids))
            db.execute('INSERT OR REPLACE INTO wiki_threads VALUES (?,?)',(thread_id,encoded(ids)))

    def thread_has_rules(self, thread_id):
        with self.connect() as db:
            row = db.execute('SELECT rules FROM wiki_threads WHERE thread_id=?',(thread_id,)).fetchone()
            return bool(row and json.loads(row[0]))
