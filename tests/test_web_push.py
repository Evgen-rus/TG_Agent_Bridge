import base64
import json
import time
from types import SimpleNamespace

import pytest

from agentbridge.web.push import PushService, validate_subscription
from agentbridge.web.store import WebError, WebStore, digest


def subscription(endpoint='https://fcm.googleapis.com/fcm/send/device'):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    return {'endpoint': endpoint, 'keys': {'p256dh': encode(public), 'auth': encode(b'a' * 16)}}


@pytest.fixture
def push(tmp_path):
    store = WebStore(tmp_path / 'access.sqlite3')
    with store.connect() as db:
        db.execute("INSERT INTO users VALUES (1,'owner','Owner','','','owner','[]',0)")
        db.execute("INSERT INTO users VALUES (2,'rick','Rick','','','rick','[]',0)")
        db.execute("INSERT INTO sessions VALUES (?,1,'rick','csrf',?)", (digest('token'), time.time() + 3600))
        db.execute("INSERT INTO sessions VALUES (?,2,'rick','csrf',?)", (digest('other'), time.time() + 3600))
    calls = []
    service = PushService(store, sender=lambda sub, data: calls.append((sub, json.loads(data))))
    return SimpleNamespace(store=store, service=service, calls=calls, user=store.session('token', 'rick'))


@pytest.mark.parametrize('endpoint', [
    'http://fcm.googleapis.com/device', 'https://127.0.0.1/device',
    'https://fcm.googleapis.com.evil.test/device', 'https://evil.test/device',
    'https://fcm.googleapis.com:444/device', 'https://user@fcm.googleapis.com/device',
    'https://fcm.googleapis.com/device#secret', 'https://fcm.googleapis.com/',
    'https://evil.push.services.mozilla.com.evil.test/device',
    'https://fcm.googleapis.com/\npath',
])
def test_push_endpoint_rejects_ssrf(endpoint):
    with pytest.raises(WebError):
        validate_subscription(subscription(endpoint))


@pytest.mark.parametrize('endpoint', ['https://web.push.apple.com/device', 'https://updates.push.services.mozilla.com/wpush/v2/device', 'https://updates-autopush.push.services.mozilla.com/wpush/v2/device'])
def test_push_official_services(endpoint):
    assert validate_subscription(subscription(endpoint))['endpoint'] == endpoint


def test_push_keys_and_account_ownership(push):
    sub = subscription()
    push.service.subscribe(push.user, 'token', sub)
    with pytest.raises(WebError):
        push.service.subscribe(push.store.session('other', 'rick'), 'other', sub)
    with pytest.raises(WebError):
        push.service.subscribe(push.user, 'other', subscription('https://fcm.googleapis.com/second'))
    sub['keys']['auth'] = 'x' * 20
    with pytest.raises(WebError):
        push.service.subscribe(push.user, 'token', sub)


def test_queue_survives_restart_dedupes_and_hides_client_content(push):
    push.service.subscribe(push.user, 'token', subscription())
    push.service.enqueue(1, 'answer:123', '-100123', created_at=time.time())
    restarted = PushService(push.store, sender=push.service.sender)
    restarted.enqueue(1, 'answer:123', '-100123')
    restarted.flush()
    restarted.flush()
    assert len(push.calls) == 1
    assert push.calls[0][1]['body'] == 'Ответ Рика готов. Откройте приложение.'
    assert push.calls[0][1]['url'] == '/rick/?context=-100123'
    assert len(push.calls[0][1]['tag']) == 64


def test_no_historical_notifications_on_new_subscription(push):
    old = time.time() - 60
    push.service.subscribe(push.user, 'token', subscription())
    push.service.enqueue(1, 'old', '1', created_at=old)
    push.service.flush()
    assert not push.calls
    push.service.enqueue(1, 'new', '1', 'reminder', created_at=time.time())
    push.service.flush()
    assert push.calls[0][1]['body'] == 'Время напоминания. Откройте Рика.'


@pytest.mark.parametrize('revoke', ['logout', 'expired', 'disabled', 'role'])
def test_revoked_or_expired_sessions_suppress_queued_push(push, revoke):
    push.service.subscribe(push.user, 'token', subscription())
    push.service.enqueue(1, 'answer:1', '1')
    if revoke == 'logout':
        push.store.logout('token')
    else:
        with push.store.connect() as db:
            if revoke == 'expired':
                db.execute('UPDATE sessions SET expires=0')
            elif revoke == 'disabled':
                db.execute('UPDATE users SET disabled=1')
            else:
                db.execute("UPDATE users SET role='sally'")
    push.service.flush()
    assert not push.calls


def test_delivery_rechecks_revoke_during_batch(push):
    push.service.subscribe(push.user, 'token', subscription())
    push.service.enqueue(1, 'answer:1', '1')
    push.service.enqueue(1, 'answer:2', '1')
    def sender(sub, data):
        push.calls.append(json.loads(data))
        push.store.logout('token')
    push.service.sender = sender
    push.service.flush()
    assert len(push.calls) == 1


def test_retry_backoff_invalid_endpoint_and_unsubscribe(push):
    push.service.subscribe(push.user, 'token', subscription())
    push.service.enqueue(1, 'answer:1', '1')
    calls = []
    def fail(sub, data):
        calls.append(data)
        raise TimeoutError('transport details must not be logged')
    push.service.sender = fail
    push.service.flush()
    push.service.flush()
    assert len(calls) == 1
    with push.store.connect() as db:
        row = db.execute('SELECT * FROM web_push_queue').fetchone()
        assert row['attempts'] == 1 and row['next_attempt'] > time.time()
        db.execute('UPDATE web_push_queue SET next_attempt=0')
    push.service.sender = lambda sub, data: SimpleNamespace(status_code=410)
    push.service.flush()
    with push.store.connect() as db:
        assert not db.execute('SELECT 1 FROM web_push_subscriptions').fetchone()
    push.service.subscribe(push.user, 'token', subscription())
    push.service.enqueue(1, 'answer:2', '1')
    push.service.unsubscribe(push.user, 'token')
    push.service.sender = fail
    push.service.flush()
    assert len(calls) == 1


def test_vapid_key_is_persistent_and_transport_disables_redirects(push, monkeypatch):
    config = push.service.config(push.user, 'token')
    assert config == PushService(push.store).config(push.user, 'token')
    assert set(config) == {'public_key', 'subscribed'}
    assert config['subscribed'] is False
    assert len(base64.urlsafe_b64decode(config['public_key'] + '==')) == 65
    calls = []
    import requests
    def fake_post(self, url, **kwargs):
        calls.append((url, kwargs, self.trust_env))
        return SimpleNamespace(status_code=201, text='', headers={})
    monkeypatch.setattr(requests.Session, 'post', fake_post)
    push.service._send(subscription(), '{"body":"test"}')
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['timeout'] == 5
    assert calls[0][2] is False


def test_reminder_click_identifies_the_actual_reminder_without_lock_screen_text(push):
    push.service.subscribe(push.user,'token',subscription())
    push.service.enqueue(1,'reminder:10','','reminder')
    push.service.flush()
    assert push.calls[0][1]['url']=='/rick/?context=&reminder=10'
    assert push.calls[0][1]['body']=='Время напоминания. Откройте Рика.'
