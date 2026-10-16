from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

from .store import WebError, digest


def validate_subscription(value):
    if not isinstance(value, dict):
        raise WebError(400, 'Некорректная подписка')
    endpoint = value.get('endpoint')
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in endpoint):
        raise WebError(400, 'Некорректный адрес уведомлений')
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ''
        known = host in ('fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com')
        known = known or bool(re.fullmatch(r'[a-z0-9-]+\.push\.services\.mozilla\.com', host))
        valid = known and url.scheme == 'https' and url.port in (None, 443) and not url.username and not url.password and not url.fragment and bool(url.path.strip('/'))
    except ValueError:
        valid = False
    if not valid:
        raise WebError(400, 'Этот сервис уведомлений не поддерживается')
    keys = value.get('keys')
    if not isinstance(keys, dict):
        raise WebError(400, 'Некорректные ключи подписки')
    for name, size in (('auth', 16), ('p256dh', 65)):
        key = keys.get(name)
        try:
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}={0,2}', key):
                raise ValueError()
            raw = base64.urlsafe_b64decode(key + '=' * (-len(key) % 4))
            if len(raw) != size or (name == 'p256dh' and raw[0] != 4):
                raise ValueError()
        except ValueError:
            raise WebError(400, 'Некорректные ключи подписки') from None
    return {'endpoint': endpoint, 'keys': {k: keys[k] for k in ('auth', 'p256dh')}}


class PushService:
    def __init__(self, store, key_path: Path | None = None, sender=None):
        self.store = store
        self.key_path = key_path or store.path.parent / 'vapid-private.pem'
        self.sender = sender or self._send
        self._key_lock = threading.Lock()
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                    id INTEGER PRIMARY KEY, endpoint TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL, session_hash TEXT NOT NULL,
                    subscription TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_push_events (
                    user_id INTEGER NOT NULL, event_key TEXT NOT NULL,
                    PRIMARY KEY(user_id,event_key));
                CREATE TABLE IF NOT EXISTS web_push_queue (
                    id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL,
                    event_key TEXT NOT NULL, payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(subscription_id,event_key));
            ''')

    def config(self, user=None, session_token=None):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        with self._key_lock:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.key_path.exists():
                key = ec.generate_private_key(ec.SECP256R1())
                encoded = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                try:
                    fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, 'wb') as output:
                        output.write(encoded)
            key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        result = {'public_key': base64.urlsafe_b64encode(public).decode().rstrip('=')}
        if user is not None and session_token is not None:
            with self.store.connect() as db:
                result['subscribed'] = bool(db.execute('SELECT 1 FROM web_push_subscriptions WHERE user_id=? AND session_hash=?',
                    (user['id'], digest(session_token))).fetchone())
        return result

    def subscribe(self, user, session_token, subscription):
        current = self.store.session(session_token, 'rick')
        if current['id'] != user['id']:
            raise WebError(403, 'Нет доступа')
        subscription = validate_subscription(subscription)
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM web_push_subscriptions WHERE endpoint=?', (subscription['endpoint'],)).fetchone()
            if old and old['user_id'] != user['id']:
                raise WebError(409, 'Отключите прежнюю подписку на этом устройстве и включите снова')
            if old and old['session_hash'] != digest(session_token):
                db.execute('DELETE FROM web_push_queue WHERE subscription_id=?', (old['id'],))
                db.execute('DELETE FROM web_push_subscriptions WHERE id=?', (old['id'],))
            db.execute('''INSERT INTO web_push_subscriptions(endpoint,user_id,session_hash,subscription,created)
                VALUES (?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET subscription=excluded.subscription''',
                (subscription['endpoint'], user['id'], digest(session_token), json.dumps(subscription), now))
        return {'subscribed': True}

    def unsubscribe(self, user, session_token):
        with self.store.connect() as db:
            rows = db.execute('SELECT id FROM web_push_subscriptions WHERE user_id=? AND session_hash=?', (user['id'], digest(session_token))).fetchall()
            for row in rows:
                db.execute('DELETE FROM web_push_queue WHERE subscription_id=?', (row['id'],))
                db.execute('DELETE FROM web_push_subscriptions WHERE id=?', (row['id'],))
        return {'subscribed': False}

    def enqueue(self, user_id, event_key, context, kind='answer', created_at=None):
        if kind not in ('answer', 'reminder') or not isinstance(event_key, str) or not 1 <= len(event_key) <= 200:
            raise ValueError('Invalid push event')
        now = time.time()
        destination = {'context':str(context)}
        if kind=='reminder' and event_key.startswith('reminder:') and event_key[9:].isdigit():
            destination['reminder'] = event_key[9:]
        payload = json.dumps({'title': 'Рик', 'body': 'Время напоминания. Откройте Рика.' if kind == 'reminder' else 'Ответ Рика готов. Откройте приложение.',
                              'url': '/rick/?' + urlencode(destination), 'tag': digest(event_key)}, ensure_ascii=False)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('INSERT OR IGNORE INTO web_push_events VALUES (?,?)', (user_id, event_key)).rowcount:
                return
            rows = db.execute('''SELECT p.id FROM web_push_subscriptions p JOIN sessions s ON s.hash=p.session_hash
                JOIN users u ON u.id=p.user_id WHERE p.user_id=? AND s.user_id=p.user_id
                AND s.product='rick' AND s.expires>? AND u.disabled=0 AND u.role IN ('owner','rick') AND p.created<=?''',
                (user_id, now, created_at if created_at is not None else now)).fetchall()
            db.executemany('INSERT OR IGNORE INTO web_push_queue(subscription_id,event_key,payload,next_attempt) VALUES (?,?,?,?)',
                           [(r['id'], event_key, payload, now) for r in rows])

    def _send(self, subscription, payload):
        from pywebpush import webpush
        import requests

        class NoRedirectSession(requests.Session):
            def post(self, url, **kwargs):
                kwargs['allow_redirects'] = False
                return super().post(url, **kwargs)

        # Encryption and VAPID signing are protocol work, kept in the maintained library.
        with NoRedirectSession() as session:
            session.trust_env = False
            return webpush(subscription_info=subscription, data=payload, vapid_private_key=str(self.key_path),
                           vapid_claims={'sub': 'https://agentbridge.app'}, requests_session=session, timeout=5, ttl=3600)

    def flush(self):
        # One runtime worker owns delivery. A crash after send may repeat; notification tags collapse it.
        with self.store.connect() as db:
            db.execute('''DELETE FROM web_push_queue WHERE subscription_id IN (
                SELECT p.id FROM web_push_subscriptions p LEFT JOIN sessions s ON s.hash=p.session_hash
                LEFT JOIN users u ON u.id=p.user_id WHERE s.hash IS NULL OR s.user_id!=p.user_id
                OR s.product!='rick' OR s.expires<=? OR u.id IS NULL OR u.disabled!=0 OR u.role NOT IN ('owner','rick'))''', (time.time(),))
            db.execute('DELETE FROM web_push_queue WHERE subscription_id NOT IN (SELECT id FROM web_push_subscriptions)')
            rows = db.execute('''SELECT q.*,p.subscription FROM web_push_queue q JOIN web_push_subscriptions p ON p.id=q.subscription_id
                WHERE q.state='pending' AND q.next_attempt<=? ORDER BY q.id LIMIT 5''', (time.time(),)).fetchall()
        for row in rows:
            # Recheck immediately before each send, including revocation during this batch.
            with self.store.connect() as db:
                active = db.execute('''SELECT 1 FROM web_push_subscriptions p JOIN sessions s ON s.hash=p.session_hash
                    JOIN users u ON u.id=p.user_id JOIN web_push_queue q ON q.subscription_id=p.id
                    WHERE q.id=? AND s.user_id=p.user_id AND s.product='rick' AND s.expires>? AND u.disabled=0
                    AND u.role IN ('owner','rick')''', (row['id'], time.time())).fetchone()
            if not active:
                continue
            status = None
            try:
                subscription = validate_subscription(json.loads(row['subscription']))
                response = self.sender(subscription, row['payload'])
                status = getattr(response, 'status_code', 201)
            except Exception as error:
                status = getattr(getattr(error, 'response', None), 'status_code', None)
            with self.store.connect() as db:
                if status in (404, 410):
                    db.execute('DELETE FROM web_push_queue WHERE subscription_id=?', (row['subscription_id'],))
                    db.execute('DELETE FROM web_push_subscriptions WHERE id=?', (row['subscription_id'],))
                elif status is not None and 200 <= status < 300:
                    db.execute("UPDATE web_push_queue SET state='sent' WHERE id=?", (row['id'],))
                else:
                    attempts = row['attempts'] + 1
                    db.execute('UPDATE web_push_queue SET attempts=?,next_attempt=?,state=? WHERE id=?',
                        (attempts, time.time() + min(3600, 30 * 2 ** min(attempts, 7)), 'failed' if attempts >= 8 else 'pending', row['id']))
