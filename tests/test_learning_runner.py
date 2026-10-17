"""Offline extraction acceptance paths; no Codex or Telegram connections."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from agentbridge.learning.runner import run_learning, validate_candidates
from agentbridge.learning.sources import read_sources, timestamp, _source
from agentbridge.learning.store import LearningStore
from agentbridge.agents.codex import validate_structured_output_schema
from agentbridge.learning.provider import SCHEMA
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.sales.storage import SalesStore

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()


def source(ident, text='Исправление: всегда уточняй недостающий срок', context='1'):
    return dict(source_id=str(ident), version='v1', text=text, context=context,
                author='Команда', date='2026-10-16T00:00:00+00:00', url='', scope='chat')


def candidate(ids, **changes):
    return dict(title='Уточнение срока', situation='Срок не указан', behavior='Спроси срок',
                rationale='Команда исправила ответ', keywords=['срок'], source_ids=ids,
                scope='chat', context='1', previous_id=None, **changes)


class Fake:
    def __init__(self, payload=None, fail=False):
        self.payload = payload or {'candidates': []}
        self.fail = fail
        self.calls = []

    def extract(self, sources):
        self.calls.append(sources)
        if self.fail:
            raise RuntimeError('fake outage')
        return self.payload


def test_schema():
    validate_structured_output_schema(SCHEMA)
    assert _source('hi','Привет!','1','Команда',NOW)==[]
    assert _source('thanks','Спасибо, но сначала уточни дату','1','Команда',NOW)


def test_journal_no_lessons_restart_edit_and_empty(tmp_path):
    store = LearningStore(tmp_path / 'db.sqlite3')
    rows = [source(1)]
    provider = Fake()
    assert run_learning(store, provider, lambda floor: rows, now=NOW)['processed'] == 1
    assert run_learning(store, provider, lambda floor: rows, now=NOW + 40 * 86400)['batches'] == 0
    assert len(provider.calls) == 1
    rows[0]['version'] = 'v2'
    assert run_learning(store, provider, lambda floor: rows, now=NOW)['processed'] == 1
    assert store.initial_cutoff(NOW + 90 * 86400) == NOW - 14 * 86400
    assert run_learning(store, provider, lambda floor: [], now=NOW)['batches'] == 0


def test_failed_provider_retries_and_candidates_commit_atomically(tmp_path):
    store = LearningStore(tmp_path / 'db.sqlite3')
    rows = [source(1)]
    with pytest.raises(RuntimeError):
        run_learning(store, Fake(fail=True), lambda floor: rows, now=NOW)
    assert store.sources_processed([('1', 'v1')]) == set()
    result = run_learning(store, Fake({'candidates': [candidate(['1'])]}), lambda floor: rows, now=NOW)
    assert result['candidates'] == 1
    assert len(store.snapshot()['candidates']) == 1
    assert run_learning(store, Fake(), lambda floor: rows, now=NOW)['batches'] == 0


def test_limits_context_isolation_exact_duplicates(tmp_path):
    store = LearningStore(tmp_path / 'db.sqlite3')
    rows = [source(i, text=str(i) + 'x' * 18000, context=str(i % 2)) for i in range(12)]
    rows.insert(1, source('duplicate', text=rows[0]['text'], context=rows[0]['context']))
    provider = Fake()
    result = run_learning(store, provider, lambda floor: rows, now=NOW)
    assert result['batches'] == 10
    assert result['remaining'] == 2
    assert result['processed'] == 11
    assert all(sum(len(s['text']) for s in packet) <= 20000 for packet in provider.calls)
    assert all(len({s['context'] for s in packet}) == 1 for packet in provider.calls)
    assert run_learning(store, provider, lambda floor: rows, now=NOW)['remaining'] == 0


def test_unknown_source_and_cross_context_do_not_commit(tmp_path):
    store = LearningStore(tmp_path / 'db.sqlite3')
    for bad in [candidate(['missing']), dict(candidate(['1']), context='2')]:
        with pytest.raises(ValueError):
            run_learning(store, Fake({'candidates': [bad]}), lambda floor: [source(1)], now=NOW)
    assert not store.sources_processed([('1', 'v1')])


def test_long_sources_split_without_truncation():
    text = 'a' * 40001
    parts = _source('event:1', text, '1', 'Сергей', '2026-10-16T00:00:00+00:00')
    assert ''.join(s['text'] for s in parts) == text
    assert len({s['source_id'] for s in parts}) == 3
    assert len({s['version'] for s in parts}) == 1
    assert all(len(s['text']) <= 18000 for s in parts)


def test_rick_saved_owner_feedback_and_web_sources(tmp_path):
    path = tmp_path / 'rick.sqlite3'
    ChatThreadStore(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO telegram_messages(update_id,chat_id,message_id,sender_name,telegram_date,text,role,processing_status,created_at) VALUES(1,123,1,'Евгений',?,'Исправь стиль','owner','processed',?)", ('2026-10-16T10:00:00+07:00',) * 2)
        db.execute("INSERT INTO owner_query_prompts(question,status,created_at) VALUES('Объясняй конкретно','answered','2026-10-16T00:00:00+00:00')")
    web = tmp_path / 'web.sqlite3'
    from agentbridge.web.store import WebStore
    WebStore(web)
    with sqlite3.connect(web) as db:
        db.execute("INSERT INTO users(id,login,name,salt,password,role,scopes) VALUES(1,'d','Дмитрий','','','member','{}')")
        db.execute("INSERT INTO jobs(user_id,product,context,key,payload,state,result,created,updated) VALUES(1,'rick','123','a',?,'done','Понял',?,?)", (json.dumps({'text': 'Указывай основание'}), NOW - 10, NOW))
        db.execute("INSERT INTO jobs(user_id,product,context,key,payload,state,result,created,updated) VALUES(1,'sally','0','b',?,'done','Салли',?,?)", (json.dumps({'text': 'Другая команда'}), NOW - 10, NOW))
    rows = read_sources(path, 'rick', NOW - 14 * 86400, web)
    assert any(s['source_id'].startswith('owner_query_prompts:') for s in rows)
    assert any(s['author'] == 'Дмитрий' for s in rows)
    assert not any('Другая команда' in s['text'] for s in rows)
    assert next(s for s in rows if s['source_id'].startswith('telegram_messages:'))['context'] == ''


def test_sales_source_readiness_cutoff_and_topic(tmp_path):
    path = tmp_path / 'sales.sqlite3'
    SalesStore(path)
    with sqlite3.connect(path) as db:
        for i, date, kind in [(1, '2026-10-16T00:00:00+00:00', 'text'),
                               (2, '2026-10-16T00:00:00+00:00', 'image_pending'),
                               (3, '2026-09-01T00:00:00+00:00', 'text')]:
            db.execute('INSERT INTO events(id,update_id,chat_id,topic_id,message_id,author_id,author,date,text,source) VALUES(?,?, -100,7,?,1,\'Сергей\',?,\'Исправляю правило\',?)', (i, i, i, date, kind))
    rows = read_sources(path, 'sally', NOW - 14 * 86400)
    assert [s['source_id'] for s in rows] == ['events:1']
    assert rows[0]['context'] == '7'


def test_timestamp_respects_offsets():
    assert timestamp('2026-10-16T07:00:00+07:00') == timestamp('2026-10-16T00:00:00+00:00')


def test_duplicate_in_later_run_needs_no_ai(tmp_path):
    store = LearningStore(tmp_path / 'db.sqlite3')
    first = source(1)
    provider = Fake()
    run_learning(store, provider, lambda floor: [first], now=NOW)
    result = run_learning(store, provider, lambda floor: [first, source(2)], now=NOW)
    assert result['processed'] == 1
    assert result['batches'] == 0
    assert len(provider.calls) == 1


def test_learning_rule_conflicts_are_relevant_and_bounded(tmp_path):
    from agentbridge.learning.provider import LearningProvider
    rules = [dict(id=i, title='Срок', situation='Уточнение срока', behavior='Спросить срок' * 30,
                  scope='global', context='', keywords=['срок'], sources=['private-history' * 5000])
             for i in range(50)]
    rules.append(dict(id=100, title='Срок', situation='Срок', behavior='Срок', scope='chat',
                      context='other', keywords=['срок']))
    provider = LearningProvider(model='fake', reasoning_effort='low', cwd=tmp_path,
                                codex_home=tmp_path, rules=rules)
    selected = provider.relevant_rules([source(1)])
    assert len(selected) <= 10
    assert len(json.dumps(selected, ensure_ascii=False)) <= 6100
    assert all('sources' not in rule and rule['id'] != 100 for rule in selected)


def test_internal_sources_without_telegram_ids_and_real_group_links(tmp_path):
    path = tmp_path / 'rick.sqlite3'
    ChatThreadStore(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO internal_context_messages(telegram_chat_id,chat_name,sender_name,message_text,created_at) VALUES(-100123,'Клиент','Сергей','Уточняй вопрос','2026-10-16T00:00:00+00:00')")
        db.execute("INSERT INTO telegram_messages(update_id,chat_id,message_id,sender_name,telegram_date,text,role,processing_status,created_at) VALUES(1,-100123,25,'Сергей','2026-10-16','Исправь стиль','internal','processed','2026-10-16')")
    rows = read_sources(path, 'rick', NOW - 14 * 86400)
    assert len(rows) == 2
    assert next(s for s in rows if s['source_id'].startswith('telegram_messages:'))['url'] == 'https://t.me/c/123/25'


def test_old_sales_message_edit_received_after_cutoff(tmp_path):
    path = tmp_path / 'sales.sqlite3'
    SalesStore(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO events(id,update_id,chat_id,topic_id,message_id,author_id,author,date,text,source,received_at,edit_update_id) VALUES(1,1,-100123,7,99,1,'Сергей','2026-09-01T00:00:00+00:00','Исправленный урок','text',?,2)", (NOW - 10,))
    rows = read_sources(path, 'sally', NOW - 14 * 86400)
    assert len(rows) == 1
    assert rows[0]['date'] == '2026-09-01T00:00:00+00:00'
    assert rows[0]['url'] == 'https://t.me/c/123/99'


def test_cli_lock_rejects_same_product_and_releases(tmp_path):
    from agentbridge.learning.__main__ import product_lock
    lock = tmp_path / 'sally-learning.lock'
    with product_lock(lock):
        with pytest.raises(RuntimeError, match='learning_already_running'):
            with product_lock(lock):
                pytest.fail('same-product overlapping extraction permitted')
        with product_lock(tmp_path / 'rick-learning.lock'):
            pass
    with product_lock(lock):
        pass
