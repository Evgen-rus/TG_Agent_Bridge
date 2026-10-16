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
            ''')
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
        if user['role'] != product:
            return False
        return context is None or str(context) in json.loads(user['scopes'])

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
            if db.execute("SELECT 1 FROM jobs WHERE user_id=? AND state IN ('pending','running')", (user['id'],)).fetchone():
                raise WebError(409, 'Дождитесь завершения предыдущего запроса')
            now = time.time()
            return db.execute('''INSERT INTO jobs(user_id,product,context,key,payload,created,updated)
                VALUES (?,?,?,?,?,?,?)''', (user['id'], product, context, key, encoded, now, now)).lastrowid

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

    def interrupted(self, product):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET state='uncertain',result=? WHERE product=? AND state='running'""",
                       ('Обработка была прервана. Результат не подтверждён; проверьте историю перед повтором.', product))

    def history(self, user, product, context):
        if not self.allowed(user, product, context):
            raise WebError(403, 'Нет доступа')
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT id,payload,state,result,created FROM jobs
                WHERE user_id=? AND product=? AND context=? ORDER BY id DESC LIMIT 30''',
                (user['id'], product, context))][::-1]
