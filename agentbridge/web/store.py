from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


class WebError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class WebStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, salt TEXT NOT NULL, password TEXT NOT NULL,
                    role TEXT NOT NULL, scopes TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS invites (
                    hash TEXT PRIMARY KEY, login TEXT NOT NULL, name TEXT NOT NULL,
                    role TEXT NOT NULL, scopes TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, product TEXT NOT NULL,
                    csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (key TEXT NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_key ON attempts(key,at);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, product TEXT NOT NULL,
                    context TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', result TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(user_id,product,key));
                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, product TEXT NOT NULL,
                    context TEXT NOT NULL, filename TEXT NOT NULL, kind TEXT NOT NULL,
                    mime TEXT NOT NULL, path TEXT NOT NULL, created REAL NOT NULL,
                    file_id TEXT NOT NULL DEFAULT '', event_id INTEGER, job_id INTEGER,
                    requested INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS web_delivery (
                    job_id INTEGER NOT NULL, part INTEGER NOT NULL,
                    state TEXT NOT NULL, message_id INTEGER, file_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(job_id,part));
            ''')
            if 'prepared' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN prepared TEXT NOT NULL DEFAULT ''")
            if 'removed' not in {r[1] for r in db.execute('PRAGMA table_info(media)')}:
                db.execute("ALTER TABLE media ADD COLUMN removed TEXT NOT NULL DEFAULT ''")
            columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
            for name, declaration in [('task_id', 'INTEGER'), ('revision', 'INTEGER NOT NULL DEFAULT 0'),
                                      ('action', "TEXT NOT NULL DEFAULT ''"), ('refinement', "TEXT NOT NULL DEFAULT ''"),
                                      ('can_confirm', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {declaration}')
        if os.name=='posix':
            path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def allowed(user, product, context=None):
        if not user or user['disabled'] or product not in ('rick', 'sally'):
            return False
        if user['role'] == 'owner':
            return True
        return user['role'] == product

    def invite(self, login, scopes):
        roles = {'evgen': ('Евгений', 'owner'), 'dmitry': ('Дмитрий', 'rick'), 'sergey': ('Сергей', 'sally')}
        if login not in roles or not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
            raise WebError(400, 'Некорректное приглашение')
        name, role = roles[login]
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            if db.execute('SELECT 1 FROM users WHERE login=? AND disabled=0', (login,)).fetchone():
                raise WebError(409, 'Этот пользователь уже зарегистрирован')
            db.execute('UPDATE invites SET used=1 WHERE login=?', (login,))
            db.execute('INSERT INTO invites VALUES (?,?,?,?,?,?,0)',
                       (digest(token), login, name, role, json.dumps(scopes), time.time()+86400))
        return token

    def enroll(self, token, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise WebError(400, 'Пароль должен содержать от 12 до 128 символов')
        salt = secrets.token_hex(16)
        hashed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM invites WHERE hash=? AND used=0 AND expires>?', (digest(token), time.time())).fetchone()
            if not row:
                raise WebError(403, 'Приглашение недействительно или уже использовано')
            existing = db.execute('SELECT * FROM users WHERE login=?', (row['login'],)).fetchone()
            if existing and not existing['disabled']:
                raise WebError(409, 'Пользователь уже зарегистрирован')
            if existing:
                db.execute('UPDATE users SET salt=?,password=?,role=?,scopes=?,disabled=0 WHERE id=?',
                           (salt,hashed,row['role'],row['scopes'],existing['id']))
            else:
                db.execute('INSERT INTO users(login,name,salt,password,role,scopes) VALUES (?,?,?,?,?,?)',
                           (row['login'], row['name'], salt, hashed, row['role'], row['scopes']))
            db.execute('UPDATE invites SET used=1 WHERE hash=?', (digest(token),))
            return row['login']

    def throttle(self, key, limit=5, seconds=900):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE at<?', (time.time()-900,))
            count = db.execute('SELECT count(*) FROM attempts WHERE key=? AND at>?', (key, time.time()-seconds)).fetchone()[0]
            if count >= limit:
                raise WebError(429, 'Слишком много попыток. Попробуйте позже.')
            db.execute('INSERT INTO attempts VALUES (?,?)', (key, time.time()))

    def login(self, login, password, product, ip):
        self.throttle('login:'+ip)
        self.throttle('account:'+login,limit=15)
        with self.connect() as db:
            user = db.execute('SELECT * FROM users WHERE login=?', (login,)).fetchone()
            salt = bytes.fromhex(user['salt']) if user else bytes(16)
            hashed = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()
            if not user or not secrets.compare_digest(hashed, user['password']) or not self.allowed(user, product):
                raise WebError(403, 'Неверные данные или нет доступа к приложению')
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', (digest(token), user['id'], product, csrf, time.time()+86400*7))
        return token

    def session(self, token, product):
        with self.connect() as db:
            row = db.execute('''SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.hash=? AND s.product=? AND s.expires>?''', (digest(token), product, time.time())).fetchone()
            if not self.allowed(row, product):
                raise WebError(401, 'Войдите в приложение')
            return dict(row)

    def logout(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE hash=?', (digest(token),))

    def enqueue(self, user, product, context, key, payload):
        if not self.allowed(user, product, context):
            raise WebError(403, 'Нет доступа к этому контексту')
        if not isinstance(key, str) or not 16 <= len(key) <= 80:
            raise WebError(400, 'Нужен ключ повторной отправки')
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM jobs WHERE user_id=? AND product=? AND key=?', (user['id'], product, key)).fetchone()
            if old:
                if old['payload'] != encoded or old['context'] != context:
                    raise WebError(409, 'Ключ уже использован для другого действия')
                return old['id']
            for item in payload.get('attachments',[]):
                media = db.execute('SELECT * FROM media WHERE id=? AND user_id=? AND product=? AND context=? AND job_id IS NULL',
                    (item,user['id'],product,context)).fetchone()
                if not media or media['removed'] or not Path(media['path']).is_file():
                    raise WebError(409,'Вложение недоступно или уже использовано')
            if db.execute("SELECT 1 FROM jobs WHERE user_id=? AND state IN ('pending','running','waiting','review')", (user['id'],)).fetchone():
                raise WebError(409, 'Дождитесь завершения предыдущего запроса')
            now = time.time()
            job = db.execute('''INSERT INTO jobs(user_id,product,context,key,payload,created,updated)
                VALUES (?,?,?,?,?,?,?)''', (user['id'], product, context, key, encoded, now, now)).lastrowid
            for item in payload.get('attachments',[]):
                db.execute('UPDATE media SET job_id=? WHERE id=?',(job,item))
            return job

    def claim(self, product):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE product=? AND state='pending' ORDER BY id LIMIT 1", (product,)).fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), row['id']))
                user = db.execute('SELECT * FROM users WHERE id=?', (row['user_id'],)).fetchone()
                return dict(row), dict(user)

    def finish(self, job_id, state, result):
        with self.connect() as db:
            db.execute('UPDATE jobs SET state=?,result=?,updated=? WHERE id=?', (state, result, time.time(), job_id))

    def reminder_review(self, job_id, task_id, text, can_confirm):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET task_id=?,state='review',result=?,can_confirm=?,
                revision=revision+1,updated=? WHERE id=?""", (task_id,text,int(can_confirm),time.time(),job_id))

    def reminder_action(self, user, context, job_id, revision, action, text='', product='rick'):
        if not self.allowed(user,product,context) or (product=='rick' and user['role']!='owner'):
            raise WebError(403,'Напоминания доступны владельцу')
        if type(job_id) is not int or type(revision) is not int or action not in ('confirm','cancel','refine'):
            raise WebError(400,'Некорректное подтверждение')
        if not isinstance(text,str) or len(text)>6000 or (action=='refine' and not text.strip()):
            raise WebError(400,'Напишите уточнение до 6000 символов')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=? AND product=? AND context=?",
                             (job_id,user['id'],product,context)).fetchone()
            if not job or not job['task_id']:
                raise WebError(404,'Напоминание недоступно')
            if job['revision'] != revision:
                raise WebError(409,'Карточка изменилась. Обновите диалог')
            if job['state'] != 'review':
                if job['action']==action and job['refinement']==text.strip():
                    return job_id
                raise WebError(409,'Действие уже принято. Обновите диалог')
            if action=='confirm' and not job['can_confirm']:
                raise WebError(409,'Сначала уточните время и текст напоминания')
            db.execute("UPDATE jobs SET state='pending',action=?,refinement=?,updated=? WHERE id=?",
                       (action,text.strip(),time.time(),job_id))
        return job_id

    def interrupted(self, product):
        with self.connect() as db:
            db.execute('UPDATE media SET requested=1 WHERE product=? AND requested=2',(product,))
            if product in ('rick','sally'):
                # Fixed reminder actions are replayable; arbitrary model turns are not.
                db.execute("""UPDATE jobs SET state='pending' WHERE product=? AND state='running'
                    AND task_id IS NOT NULL AND action IN ('confirm','cancel')""",(product,))
                db.execute("""UPDATE jobs SET state='review',can_confirm=0,revision=revision+1,
                    result='Уточнение было прервано. Уточните дату, время и текст ещё раз.'
                    WHERE product=? AND state='running' AND task_id IS NOT NULL AND action='refine'""",(product,))
            if product == 'sally':
                # Only Sally's persisted ingest and acknowledged sends can safely resume.
                db.execute("""UPDATE jobs SET state='pending' WHERE product='sally' AND state='running'
                    AND NOT EXISTS(SELECT 1 FROM web_delivery d WHERE d.job_id=jobs.id AND d.state='sending')""")
            db.execute("""UPDATE jobs SET state='uncertain',result=? WHERE product=? AND state='running'""",
                       ('Обработка была прервана. Результат не подтверждён; проверьте историю перед повтором.', product))

    def history(self, user, product, context):
        if not self.allowed(user, product, context):
            raise WebError(403, 'Нет доступа')
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('''SELECT j.id,j.payload,j.state,j.result,j.created,j.prepared,j.task_id,j.revision,j.can_confirm,u.name AS author FROM jobs j JOIN users u ON u.id=j.user_id
                WHERE j.product=? AND j.context=? ORDER BY j.id DESC LIMIT 30''',
                (product, context))][::-1]
            for row in rows:
                row['attachments']=[dict(r) for r in db.execute('SELECT id,filename,kind,mime FROM media WHERE job_id=?',(row['id'],))]
            return rows

    def media(self, user, product, context, ids):
        if not isinstance(ids, list) or len(ids) > 8 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise WebError(400, 'Не более 8 разных вложений')
        result = []
        with self.connect() as db:
            for ident in ids:
                row = db.execute('SELECT * FROM media WHERE id=? AND user_id=? AND product=? AND context=?',
                                 (ident, user['id'], product, context)).fetchone()
                if not row:
                    raise WebError(404, 'Вложение недоступно')
                if row['removed']:
                    from agentbridge.retention import EXPIRED_MESSAGE, QUOTA_MESSAGE
                    raise WebError(410, EXPIRED_MESSAGE if row['removed']=='expired' else QUOTA_MESSAGE)
                result.append(dict(row))
        return result

    def delivery(self, job_id, part, **values):
        with self.connect() as db:
            if values:
                db.execute('''INSERT INTO web_delivery(job_id,part,state,message_id,file_id) VALUES (?,?,?,?,?)
                    ON CONFLICT(job_id,part) DO UPDATE SET state=excluded.state,
                    message_id=excluded.message_id,file_id=excluded.file_id''',
                    (job_id, part, values['state'], values.get('message_id'), values.get('file_id', '')))
            row = db.execute('SELECT * FROM web_delivery WHERE job_id=? AND part=?', (job_id, part)).fetchone()
            return dict(row) if row else None
