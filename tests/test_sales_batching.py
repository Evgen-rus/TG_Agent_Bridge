from types import SimpleNamespace as NS

import pytest

from agentbridge.sales.storage import SalesStore


def add(store, update, topic=10, text='Сообщение менеджера', *, source='telegram_text',
        respond=True, audio=None):
    return store.ingest(update_id=update, chat_id=-100, topic_id=topic, title=f'Сделка {topic}',
        message_id=update, author_id=7, author='Менеджер', date='2026-10-12T10:00:00+00:00',
        text=text, source=source, respond=respond, audio=audio)


def clock(monkeypatch, start=1000):
    now = [start]
    monkeypatch.setattr('agentbridge.sales.storage.time', NS(time=lambda: now[0]))
    return now


def test_batch_debounce_topics_restart_and_cutoff(monkeypatch, tmp_path):
    now = clock(monkeypatch)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    add(store, 1, text='Первое сообщение')
    now[0] = 1005
    add(store, 2, text='Второе сообщение')
    now[0] = 1008
    add(store, 3, topic=20, text='Другая сделка')

    now[0] = 1019
    assert store.pending_topics(batched=True) == []
    now[0] = 1020
    assert store.pending_topics(batched=True) == [(-100, 10)]

    # The cutoff survives restart; later messages belong to the next window.
    store = SalesStore(store.path)
    now[0] = 1021
    add(store, 4, text='Пришло после границы')
    assert [event['text'] for event in store.context(-100, 10, batch=True)['events']] == [
        'Первое сообщение', 'Второе сообщение']
    assert store.pending_topics(batched=True) == [(-100, 10)]


def test_sixty_second_cap_freezes_before_late_message(monkeypatch, tmp_path):
    now = clock(monkeypatch, 0)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    add(store, 1, text='Первая часть')
    now[0] = 10
    add(store, 2, text='Продолжение')
    now[0] = 20
    add(store, 3, text='Ещё продолжение')
    now[0] = 30
    add(store, 4, text='После шестидесяти секунд')
    now[0] = 40
    add(store, 5, text='Пятая часть')
    now[0] = 50
    add(store, 6, text='Шестая часть')
    now[0] = 60
    add(store, 7, text='После шестидесяти секунд')
    assert store.pending_topics(batched=True) == [(-100, 10)]
    assert [event['id'] for event in store.context(-100, 10, batch=True)['events']] == [1, 2, 3, 4, 5, 6]
    events = store.context(-100, 10, batch=True)['events']
    store.complete(-100, 10, events, dict(answer='Пакет обработан', facts=[], next_action='', goal='', basis=[]))
    now[0] = 75
    assert store.pending_topics(batched=True) == [(-100, 10)]
    assert [event['id'] for event in store.context(-100, 10, batch=True)['events']] == [7]


def test_batch_keeps_all_messages_above_legacy_ten_event_limit(monkeypatch, tmp_path):
    now = clock(monkeypatch, 500)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    for update in range(1, 12):
        now[0] = 500 + update / 10
        add(store, update)
    now[0] = 516.2
    assert store.pending_topics(batched=True) == [(-100, 10)]
    assert len(store.context(-100, 10)['events']) == 10
    assert len(store.context(-100, 10, batch=True)['events']) == 11


def test_pending_audio_and_document_hold_whole_frozen_batch(monkeypatch, tmp_path):
    now = clock(monkeypatch, 700)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    add(store, 1, text='Описание сделки')
    add(store, 2, text='Подпись', source='audio_pending', audio={
        'file_id': 'opaque', 'routing': {'bot_username': 'sali_bot', 'addressed_elsewhere': False}})
    add(store, 3, text='метаданные файла', source='text_document_pending')
    now[0] = 715
    assert store.pending_topics(batched=True) == []
    assert store.context(-100, 10, batch=True)['events'] == []

    audio = store.awaiting_audio(-100)[0]
    assert store.audio_transcribed(audio['event_id'], 'Клиент готов провести встречу')
    store.hydrate_document(3, 'Полный текст документа')
    assert store.pending_topics(batched=True) == [(-100, 10)]
    events = store.context(-100, 10, batch=True)['events']
    assert len(events) == 3
    assert events[1]['source'] == 'audio_transcript'
    assert events[2]['text'] == 'Полный текст документа'


def test_held_authorization_blocks_batch_until_rechecked(monkeypatch, tmp_path):
    now = clock(monkeypatch, 900)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    store.ingest(update_id=1, chat_id=-100, topic_id=10, title='Сделка', message_id=1,
        author_id=8, author='Новый участник', date='2026-10-12', text='Скрыто до проверки',
        authorization_pending=True)
    now[0] = 901
    add(store, 2, text='Обычное сообщение')
    now[0] = 916
    assert store.pending_topics(batched=True) == []
    store.authorize(1, True, 'Справка')
    assert store.pending_topics(batched=True) == [(-100, 10)]
    assert len(store.context(-100, 10, batch=True)['events']) == 2


def test_later_windows_keep_boundaries_while_old_audio_blocks(monkeypatch, tmp_path):
    now = clock(monkeypatch, 100)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    add(store, 1, source='audio_pending', audio={'file_id': 'opaque'})
    now[0] = 115
    assert store.pending_topics(batched=True) == []
    for update, receipt in enumerate(range(120, 211, 10), 2):
        now[0] = receipt
        add(store, update, text=f'Продолжение {update}')
    store.audio_transcribed(1, 'Клиент получил договор')
    first = store.context(-100, 10, batch=True)['events']
    assert [event['id'] for event in first] == [1]
    done = dict(answer='Обработано', facts=[], next_action='', goal='', basis=[])
    store.complete(-100, 10, first, done)
    assert store.pending_topics(batched=True) == [(-100, 10)]
    second = store.context(-100, 10, batch=True)['events']
    assert [event['id'] for event in second] == list(range(2, 8))
    store.complete(-100, 10, second, done)
    assert store.pending_topics(batched=True) == []
    now[0] = 225
    assert store.pending_topics(batched=True) == [(-100, 10)]
    assert [event['id'] for event in store.context(-100, 10, batch=True)['events']] == list(range(8, 12))


def test_quiet_message_extends_wait_and_duplicate_does_not(monkeypatch, tmp_path):
    now = clock(monkeypatch)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    add(store, 1)
    now[0] = 1010
    add(store, 2, text='спасибо', respond=False)
    now[0] = 1020
    assert not add(store, 2, text='спасибо', respond=False)
    assert store.pending_topics(batched=True) == []
    now[0] = 1025
    assert store.pending_topics(batched=True) == [(-100, 10)]
    assert [event['id'] for event in store.context(-100, 10, batch=True)['history']] == [2]


def test_ready_topics_retry_and_legacy_queue(monkeypatch, tmp_path):
    now = clock(monkeypatch)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    add(store, 1)
    add(store, 2, topic=20)
    now[0] = 1015
    assert store.pending_topics(batched=True) == [(-100, 10), (-100, 20)]
    store.failed(-100, 10, 1)
    assert store.pending_topics(batched=True) == [(-100, 20)]
    assert not store.context(-100, 10, batch=True)['events']
    # Migration has no historical receipt timestamps; catch-up starts immediately.
    with store.connect() as db:
        db.execute('ALTER TABLE events DROP COLUMN received_at')
        db.execute('ALTER TABLE deals DROP COLUMN batch_cutoff_id')
    reopened = SalesStore(store.path)
    assert reopened.pending_topics(batched=True) == [(-100, 20)]
    now[0] = 1315
    assert reopened.pending_topics(batched=True) == [(-100, 20), (-100, 10)]
