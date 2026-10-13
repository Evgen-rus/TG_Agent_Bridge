import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from openai_codex.errors import InvalidRequestError

from agentbridge.sales.provider import INSTRUCTIONS, SalesCodexProvider, SalesFailure, split_transcript
from agentbridge.sales.service import SalesService
from agentbridge.sales.storage import SalesStore
from test_sales_buddy import ingest


class SDK:
    starts = []
    resumes = []
    turns = []
    failure = None
    lost = False
    chunk_failure = None

    def __init__(self, config): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def thread_start(self, **kwargs):
        self.starts.append(kwargs)
        return Thread(f'thread-{len(self.starts)}')
    def thread_resume(self, thread_id, **kwargs):
        self.resumes.append(thread_id)
        if self.lost:
            raise InvalidRequestError(-32600, 'no rollout found for thread id')
        return Thread(thread_id)


class Thread:
    def __init__(self, thread_id): self.id = thread_id
    def run(self, prompt, **kwargs):
        context = json.loads(prompt)
        SDK.turns.append((self.id, context))
        if SDK.failure:
            return NS(error=NS(message=SDK.failure), final_response=None)
        if 'technical_probe' in context:
            return NS(error=None, items=[], final_response='{"ready":true}')
        if SDK.chunk_failure is not None and context.get('document_part', {}).get('index') == SDK.chunk_failure:
            raise OSError('network failure')
        facts = []
        for event in context['events']:
            for token in ('НАЧАЛО', 'СЕРЕДИНА', 'КОНЕЦ'):
                if token in event['text']:
                    facts.append(dict(field='confirmed', kind='fact', text=token,
                                      event_id=event['id'], quote=token, replaces=0))
        payload = dict(answer='Изучены источники.', facts=facts,
                       basis=[context['events'][0]['id']], next_action='', goal='')
        return NS(error=None, items=[], final_response=json.dumps(payload))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    SDK.starts, SDK.resumes, SDK.turns = [], [], []
    SDK.failure, SDK.chunk_failure, SDK.lost = None, None, False
    monkeypatch.setattr('agentbridge.sales.provider.Codex', SDK)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    def provider(store=store):
        return SalesCodexProvider(model='fake', reasoning_effort='medium', cwd=tmp_path,
            codex_home=tmp_path / 'codex', knowledge='rules', store=store)
    return store, provider


@pytest.mark.asyncio
async def test_persistent_threads_resume_restart_and_topic_isolation(setup):
    store, factory = setup
    ingest(store)
    await SalesService(store, factory()).process(-100, 10)
    first = store.thread(-100, 10)['thread_id']
    reopened = SalesStore(store.path)
    ingest(reopened, 2)
    await SalesService(reopened, factory(reopened)).process(-100, 10)
    assert SDK.resumes == [first]
    assert store.thread(-100, 10)['thread_turns'] == 2
    ingest(store, 3, topic=20, text='Другая сделка')
    await SalesService(store, factory()).process(-100, 20)
    assert store.thread(-100, 20)['thread_id'] != first
    assert SDK.turns[-1][1]['history'] == []
    assert 'Птицефабрика выбирает' not in json.dumps(SDK.turns[-1][1], ensure_ascii=False)
    assert all(not s['ephemeral'] for s in SDK.starts)


@pytest.mark.parametrize('ephemeral', [False, True])
def test_start_preserves_codex_base_and_sales_developer_rules(setup, ephemeral):
    _, factory = setup
    provider = factory()
    provider._start(SDK(provider._config()), ephemeral=ephemeral)
    started = SDK.starts[-1]
    assert 'base_instructions' not in started
    assert started['developer_instructions'] == INSTRUCTIONS + '\n\nПравила продаж:\nrules'
    assert started['ephemeral'] is ephemeral
    assert started['sandbox'].value == 'read-only'
    assert started['approval_mode'].value == 'deny_all'


@pytest.mark.asyncio
async def test_missing_thread_rebuilds_from_sqlite_only(setup):
    store, factory = setup
    ingest(store, text='НАЧАЛО')
    await SalesService(store, factory()).process(-100, 10)
    first = store.thread(-100, 10)['thread_id']
    SDK.lost = True
    ingest(store, 2, text='Продолжение')
    await SalesService(store, factory()).process(-100, 10)
    assert store.thread(-100, 10)['thread_id'] != first
    assert store.op_get('thread-reason:-100:10') == 'thread_unavailable'
    assert SDK.turns[-1][1]['facts'][0]['text'] == 'НАЧАЛО'
    assert SDK.turns[-1][1]['history'][0]['text'] == 'НАЧАЛО'


@pytest.mark.asyncio
@pytest.mark.parametrize('error,kind', [('usage_limit_exceeded Try again at 4:30 PM', 'codex_usage_limit'),
    ('401 unauthorized secret', 'codex_auth'), ('network timeout', 'codex_network')])
async def test_failures_preserve_thread_dedup_notices_and_recover(setup, error, kind, caplog):
    store, factory = setup
    ingest(store)
    await SalesService(store, factory()).process(-100, 10)
    thread = store.thread(-100, 10)['thread_id']
    ingest(store, 2)
    SDK.failure = error
    service = SalesService(store, factory(), system_chat_id=-100)
    await service.process(-100, 10)
    assert store.thread(-100, 10)['thread_id'] == thread
    assert store.op_get('codex_failure')['kind'] == kind
    assert len(store.context(-100, 10)['events']) == 1
    before = len(SDK.turns)
    assert await service.probe() is False
    assert len(SDK.turns) == before
    await service.record_failure(SalesFailure(kind))
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE delivery_key LIKE 'unavailable:%'").fetchone()[0] == 1
    state = store.op_get('codex_failure')
    state['retry_at'] = 0
    store.op_set('codex_failure', state)
    SDK.failure = None
    assert await service.probe() is True
    await service.process(-100, 10)
    assert not store.context(-100, 10)['events']
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE delivery_key LIKE 'recovered:%'").fetchone()[0] == 1
    assert 'unauthorized secret' not in caplog.text


@pytest.mark.asyncio
async def test_complete_long_document_all_parts_and_recovery_checkpoint(setup):
    store, factory = setup
    text = 'НАЧАЛО\n' + 'а' * 17000 + '\nСЕРЕДИНА\n' + 'б' * 17000 + '\nКОНЕЦ'
    ingest(store, text=text)
    SDK.chunk_failure = 1
    service = SalesService(store, factory())
    await service.process(-100, 10)
    assert not store.context(-100, 10)['facts']
    assert store.context(-100, 10)['events'][0]['text'] == text
    first_calls = len([c for _, c in SDK.turns if c.get('document_part', {}).get('index') == 0])
    SDK.chunk_failure = None
    store.op_set('codex_failure', None)
    await SalesService(SalesStore(store.path), factory()).process(-100, 10)
    context = store.context(-100, 10)
    assert {f['text'] for f in context['facts']} == {'НАЧАЛО', 'СЕРЕДИНА', 'КОНЕЦ'}
    assert all(f['quote'] in text and f['event_id'] == 1 for f in context['facts'])
    assert len([c for _, c in SDK.turns if c.get('document_part', {}).get('index') == 0]) == first_calls
    final = SDK.turns[-1][1]['events'][0]
    assert final['fully_read'] and len(final['document_analysis']) == 3
    assert sum(p['length'] for p in final['document_analysis']) == len(text)
    await service.process(-100, 10)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM recommendations').fetchone()[0] == 1
        assert db.execute('SELECT text FROM events').fetchone()[0] == text


@pytest.mark.asyncio
async def test_crash_after_turn_reuses_checkpoint_and_no_duplicate_commit(setup):
    store, factory = setup
    ingest(store)
    context = store.context(-100, 10)
    payload = await factory().analyze(context)
    count = len(SDK.turns)
    await SalesService(SalesStore(store.path), factory()).process(-100, 10)
    assert len(SDK.turns) == count
    assert not store.complete(-100, 10, context['events'], payload)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM recommendations').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_compaction_recreates_from_ledger_not_full_history(setup):
    store, factory = setup
    ingest(store, text='НАЧАЛО')
    await SalesService(store, factory()).process(-100, 10)
    with store.connect() as db:
        db.execute('UPDATE deals SET thread_turns=50')
    ingest(store, 2)
    await SalesService(store, factory()).process(-100, 10)
    assert len(SDK.starts) == 2
    assert store.op_get('thread-reason:-100:10') == 'context_compaction'
    assert SDK.turns[-1][1]['facts'][0]['text'] == 'НАЧАЛО'


def test_lossless_speaker_boundaries():
    text = ('Сергей: привет\nКлиент: да\n' * 2000) + 'Конец'
    chunks = list(split_transcript(text))
    assert ''.join(part for _, part in chunks) == text
    assert all(len(part) <= 16000 for _, part in chunks)
    assert all(text[offset:offset+len(part)] == part for offset, part in chunks)


@pytest.mark.asyncio
async def test_oversized_document_is_explicitly_incomplete_without_partial_recommendation(setup):
    store, factory = setup
    text = 'a' * (65 * 16000)
    ingest(store, text=text)
    await SalesService(store, factory()).process(-100, 10)
    with store.connect() as db:
        assert db.execute('SELECT processed FROM events').fetchone()[0] == 3
        assert db.execute('SELECT text FROM events').fetchone()[0] == text
        assert db.execute('SELECT count(*) FROM recommendations').fetchone()[0] == 0
    assert any('полный анализ не завершён' in r['text'] for r in store.deliveries())
    assert not store.pending_topics()
    assert SDK.turns == []  # Reject known oversize before spending model calls.


@pytest.mark.asyncio
async def test_sdk_cancellation_holds_lock_until_worker_finishes(setup, monkeypatch):
    import threading
    store, factory = setup
    ingest(store)
    entered, release = threading.Event(), threading.Event()
    provider = factory()
    def slow(context):
        entered.set()
        release.wait(3)
        return dict(answer='ready', facts=[], basis=[], next_action='', goal='')
    monkeypatch.setattr(provider, '_analyze', slow)
    service = SalesService(store, provider)
    task = asyncio.create_task(service.process(-100, 10))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(.05)
    assert service._lock.locked() and not task.done()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert not service._lock.locked()
    assert store.context(-100, 10)['events']
