"""Offline acceptance paths: model proposals are fakes, Telegram never connects."""
import asyncio
from datetime import datetime
import json
from types import SimpleNamespace as NS

import pytest

from agentbridge.agents.codex import validate_structured_output_schema
from agentbridge.sales.bot import SalesTelegram
from agentbridge.sales.commitments import (
    Commitments, LOCAL, SCHEMA, TASK, business_after, delivery_allowed,
    in_window, validate_updates, disable,
)
from agentbridge.sales.provider import SalesCodexProvider
from agentbridge.sales.service import SalesService
from agentbridge.sales.settings import SalesSettings
from agentbridge.sales.storage import SalesStore
from test_sales_buddy import Fake, ingest


def stamp(text):
    return datetime.fromisoformat(text).replace(tzinfo=LOCAL).timestamp()


NOW = stamp('2026-10-14T11:00:00')


@pytest.fixture
def engine(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    service = SalesService(store, Fake())
    engine = Commitments(service, -100)
    service.commitments = engine
    return engine


def proposal(event, **changes):
    return dict(id=0, kind='manager_action', party='Сергей', action='Отправить КП',
                action_key='отправить кп', state='active', event_id=event['id'],
                quote=event['text'], due_date='2026-10-12', date_basis='message_date',
                material='current', reason='Явное обещание менеджера', reminder='keep', remind_at=None, **changes)


def add(engine, number=1, topic=10, text='Завтра отправлю КП', now=NOW, **changes):
    ingest(engine.store, number, topic=topic, text=text, respond=False)
    batch = engine.batch(topic)
    row = proposal(batch['events'][-1])
    row.update(changes)
    assert engine.apply(batch, {'updates': [row]}, now=now)
    return engine.context(-100, topic)[-1]


def update(engine, ident, number, state, text, **changes):
    ingest(engine.store, number, text=text, respond=False)
    batch = engine.batch(10)
    old = next(r for r in batch['commitments'] if r['id'] == ident)
    row = proposal(batch['events'][-1])
    row.update({k: old[k] for k in ('kind', 'party', 'action', 'action_key', 'due_date')})
    row.update(id=ident, state=state)
    row.update(changes)
    assert engine.apply(batch, {'updates': [row]}, now=NOW)


def registry(engine):
    with engine.store.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM commitments ORDER BY id')]


def test_business_days_and_window():
    assert business_after(datetime.fromtimestamp(stamp('2026-10-16T23:00:00'), LOCAL), 1) == stamp('2026-10-19T10:00:00')
    assert business_after(datetime.fromtimestamp(stamp('2026-10-15T11:00:00'), LOCAL), 3) == stamp('2026-10-20T10:00:00')
    assert in_window(stamp('2026-10-14T10:00:00'))
    for value in ('2026-10-14T09:59:00', '2026-10-14T18:00:00', '2026-10-17T12:00:00'):
        assert not in_window(stamp(value))


def test_first_enable_ignores_old_events_and_does_not_replace_threads(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store)
    store.save_thread(-100, 10, 'existing-sales-thread', 'version', 'first_turn')
    original = store.thread(-100, 10)
    engine = Commitments(SalesService(store, Fake()), -100)
    assert engine.batch(10) is None
    add(engine, 2)
    reopened = Commitments(SalesService(SalesStore(store.path), Fake()), -100)
    assert reopened.batch(10) is None
    assert len(reopened.context(-100, 10)) == 1
    assert store.thread(-100, 10) == original
    assert not (tmp_path / 'agentbridge.sqlite3').exists()


def test_replayed_and_paraphrased_proposals_do_not_duplicate(engine):
    row = add(engine)
    assert engine.batch(10) is None
    ingest(engine.store, 2, text='КП обещал отправить завтра', respond=False)
    context = engine.batch(10)
    duplicate = proposal(context['events'][0])
    duplicate['action'] = 'Выслать коммерческое предложение'
    assert engine.apply(context, {'updates': [duplicate]}, now=NOW)
    assert not engine.apply(context, {'updates': [duplicate]}, now=NOW)
    assert len(registry(engine)) == 1
    assert registry(engine)[0]['id'] == row['id']


@pytest.mark.parametrize('text', ['Можем подготовить КП', 'Нужно бы проверить договор', 'Черновик: завтра отправлю КП', 'Ок'])
def test_no_updates_are_silent_and_durably_processed(engine, text):
    ingest(engine.store, text=text, respond=False)
    batch = engine.batch(10)
    assert engine.apply(batch, {'updates': []}, now=NOW)
    assert engine.batch(10) is None
    assert not registry(engine) and not engine.store.deliveries()


def test_prompt_covers_meaning_and_untrusted_sources():
    validate_structured_output_schema(SCHEMA)
    for phrase in ('НЕ отправленный черновик', 'неоднозначному', 'readable_text', 'дату загрузки', 'недоверенные'):
        assert phrase in TASK


def test_wait_without_deadline_and_historical_sources(engine):
    row = add(engine, text='КП отправил, ждём ответа', kind='client_wait', party='Клиент',
              action='Ответ клиента по КП', action_key='ответ кп', due_date=None, date_basis='none')
    assert row['next_check'] == stamp('2026-10-14T10:00:00')
    other = add(engine, 2, topic=20, text='В старом звонке клиент обещал подумать', kind='client_wait',
                due_date=None, date_basis='unknown', material='historical')
    assert other['next_check'] is None
    no_deadline = add(engine, 3, topic=30, text='Отправлю договор', due_date=None, date_basis='none')
    assert no_deadline['next_check'] is None


@pytest.mark.parametrize('mutation', ['foreign_id', 'foreign_event', 'quote', 'date', 'historical_relative', 'invented_date', 'bool_id', 'unknown_key'])
def test_untrusted_proposals_rejected_atomically(engine, mutation):
    ingest(engine.store, respond=False, text='Завтра отправлю КП')
    context = engine.batch(10)
    payload = proposal(context['events'][0])
    if mutation == 'foreign_id': payload['id'] = 999
    if mutation == 'foreign_event': payload['event_id'] = 999
    if mutation == 'quote': payload['quote'] = 'Это не цитата'
    if mutation == 'date': payload['due_date'] = '2026-02-30'
    if mutation == 'historical_relative': payload['material'] = 'historical'
    if mutation == 'invented_date': payload['due_date'] = '2027-01-20'
    if mutation == 'bool_id': payload['id'] = False
    if mutation == 'unknown_key': payload['chat_id'] = -999
    with pytest.raises(ValueError):
        engine.apply(context, {'updates': [payload]}, now=NOW)
    assert not registry(engine)
    assert engine.batch(10) is not None


def test_absolute_date_and_uncertain_image_validation(engine):
    ingest(engine.store, respond=False, text='Отправлю договор 16 октября 2026')
    context = engine.batch(10)
    payload = proposal(context['events'][0])
    payload.update(due_date='2026-10-16', date_basis='absolute')
    assert engine.apply(context, {'updates': [payload]}, now=NOW)
    assert registry(engine)[0]['next_check'] == stamp('2026-10-19T10:00:00')
    ingest(engine.store, 2, text='На картинке похоже обещали КП', source='image_transcript', respond=False)
    context = engine.batch(10)
    payload = proposal(context['events'][0])
    payload.update(due_date=None, date_basis='none')
    with pytest.raises(ValueError, match='image'):
        validate_updates({'updates': [payload]}, context)
    context['events'][0]['image_evidence'] = [dict(kind='readable_text', text=payload['quote'])]
    assert validate_updates({'updates': [payload]}, context)


@pytest.mark.parametrize('state,text', [('done', 'Да, КП отправил вчера'), ('cancelled', 'Не напоминай по этому КП'), ('paused', 'Клиент попросил пока не отправлять'), ('hold', 'Отправил, но непонятно какой из двух КП')])
def test_natural_state_updates_are_silent(engine, state, text):
    row = add(engine)
    update(engine, row['id'], 2, state, text)
    stored = registry(engine)[0]
    assert stored['state'] == ('active' if state == 'hold' else state)
    assert stored['next_check'] is None
    assert bool(stored['quiet']) == (state == 'hold')
    assert not engine.store.deliveries()
    if state in ('done', 'cancelled'):
        ingest(engine.store, 3, text='Повторное упоминание КП', respond=False)
        context = engine.batch(10)
        payload = proposal(context['events'][0])
        payload.update(id=row['id'])
        with pytest.raises(ValueError, match='transition'):
            engine.apply(context, {'updates': [payload]}, now=NOW)


def test_postpone_and_resume(engine):
    row = add(engine)
    update(engine, row['id'], 2, 'active', 'Отправлю КП 20 октября 2026',
           due_date='2026-10-20', date_basis='absolute')
    assert registry(engine)[0]['next_check'] == stamp('2026-10-21T10:00:00')
    engine.queue_due(10, NOW)
    assert not engine.store.deliveries()
    update(engine, row['id'], 3, 'paused', 'Пока пауза')
    update(engine, row['id'], 4, 'active', 'Вернулись, отправлю 22 октября 2026',
           due_date='2026-10-22', date_basis='absolute')
    assert registry(engine)[0]['next_check'] == stamp('2026-10-23T10:00:00')


def test_queue_ack_retry_daily_limit_and_quiet_after_two_checks(engine, monkeypatch):
    add(engine)
    engine.queue_due(10, NOW)
    engine.queue_due(10, NOW)
    row = engine.store.deliveries()[0]
    assert len(engine.store.deliveries()) == 1
    assert registry(engine)[0]['checks'] == 0
    assert delivery_allowed(engine.store, row, True, NOW)
    monkeypatch.setattr('agentbridge.sales.storage.time.time', lambda: NOW)
    engine.store.delivered(row['id'], 100)
    engine.store.delivered(row['id'], 100)
    assert registry(engine)[0]['checks'] == 1
    assert registry(engine)[0]['next_check'] == stamp('2026-10-19T10:00:00')
    engine.queue_due(10, NOW)
    assert not engine.store.deliveries()
    later = stamp('2026-10-19T11:00:00')
    engine.queue_due(10, later)
    row = engine.store.deliveries()[0]
    assert delivery_allowed(engine.store, row, True, later)
    monkeypatch.setattr('agentbridge.sales.storage.time.time', lambda: later)
    engine.store.delivered(row['id'], 101)
    assert registry(engine)[0]['quiet'] == 1
    engine.queue_due(10, stamp('2026-10-30T11:00:00'))
    assert not engine.store.deliveries()


def test_multiple_items_grouped_and_topics_independent(engine):
    add(engine)
    add(engine, 2, text='Завтра отправлю договор', action='Отправить договор', action_key='отправить договор')
    add(engine, 3, topic=20, text='Завтра согласую встречу', action='Согласовать встречу', action_key='встреча')
    engine.queue_due(10, NOW)
    engine.queue_due(20, NOW)
    rows = engine.store.deliveries()
    assert len(rows) == 2
    assert 'КП' in rows[0]['text'] and 'договор' in rows[0]['text']
    assert 'встречу' not in rows[0]['text'] and 'КП' not in rows[1]['text']


@pytest.mark.parametrize('reason', ['new_event', 'disabled', 'postponed', 'pending_audio'])
def test_stale_or_disabled_check_cancelled_before_delivery(engine, reason):
    row = add(engine)
    engine.queue_due(10, NOW)
    delivery = engine.store.deliveries()[0]
    if reason == 'new_event': ingest(engine.store, 2, text='КП отправил', respond=False)
    if reason == 'postponed': update(engine, row['id'], 2, 'active', 'Отправлю 20 октября 2026', due_date='2026-10-20', date_basis='absolute')
    if reason == 'pending_audio': ingest(engine.store, 2, text='', source='audio_pending', respond=False)
    assert not delivery_allowed(engine.store, delivery, reason != 'disabled', NOW)
    assert not engine.store.deliveries()
    assert registry(engine)[0]['checks'] == 0


def test_delivery_waits_outside_hours_and_authorization_or_hydration_blocks(engine):
    add(engine)
    engine.queue_due(10, NOW)
    row = engine.store.deliveries()[0]
    assert not delivery_allowed(engine.store, row, True, stamp('2026-10-14T18:00:00'))
    assert engine.store.deliveries()
    assert delivery_allowed(engine.store, row, True, stamp('2026-10-15T10:00:00'))
    ingest(engine.store, 2, text='', source='audio_pending', respond=False)
    ingest(engine.store, 3, text='После голоса', respond=False)
    assert engine.batch(10) is None
    with engine.store.connect() as db:
        db.execute("UPDATE events SET source='audio_transcript',text='КП отправил' WHERE id=2")
    assert len(engine.batch(10)['events']) == 2
    engine.apply(engine.batch(10), {'updates': []}, now=NOW)
    ingest(engine.store, 4, text='Непроверенный участник', authorization_pending=True)
    assert engine.batch(10) is None
    assert engine.contextual_check(-100, 10, NOW) is None


def test_stale_model_result_keeps_events_pending(engine):
    ingest(engine.store, text='Завтра отправлю КП', respond=False)
    context = engine.batch(10)
    ingest(engine.store, 2, text='Уже отправил', respond=False)
    assert not engine.apply(context, {'updates': [proposal(context['events'][0])]}, now=NOW)
    assert not registry(engine)
    assert len(engine.batch(10)['events']) == 2


def test_cancelled_outbox_id_can_be_reused_without_inheriting_old_metadata(engine):
    add(engine)
    engine.queue_due(10, NOW)
    old = engine.store.deliveries()[0]
    assert not delivery_allowed(engine.store, old, False, NOW)
    engine.queue_due(10, NOW)
    fresh = engine.store.deliveries()[0]
    assert delivery_allowed(engine.store, fresh, True, NOW)
    assert registry(engine)[0]['checks'] == 0


def test_resume_reconciles_without_immediate_old_followups(engine, monkeypatch):
    add(engine)
    engine.store.op_set('commitments_enabled', False)
    monkeypatch.setattr('agentbridge.sales.commitments.time.time', lambda: NOW)
    reopened = Commitments(engine.service, -100)
    reopened.queue_due(10, NOW)
    assert not reopened.store.deliveries()
    reopened.queue_due(10, stamp('2026-10-15T10:00:00'))
    assert reopened.store.deliveries()


@pytest.mark.asyncio
async def test_contextual_question_appended_once_and_stripped_when_stale(engine, monkeypatch):
    add(engine)
    class Provider(Fake):
        async def analyze_commitments(self, context):
            return {'updates': []}
    engine.service.provider = Provider()
    monkeypatch.setattr('agentbridge.sales.commitments.time.time', lambda: NOW)
    ingest(engine.store, 2, text='Сали, что делать дальше?')
    await engine.service.process(-100, 10)
    assert len(engine.store.deliveries()) == 1
    row = engine.store.deliveries()[0]
    assert 'Кстати' in row['text']
    assert 'commitments' in engine.service.provider.contexts[0]
    ingest(engine.store, 3, text='КП отправил', respond=False)
    assert delivery_allowed(engine.store, row, True, NOW)
    assert 'Кстати' not in row['text']
    assert 'Кстати' not in engine.store.deliveries()[0]['text']


@pytest.mark.asyncio
async def test_contextual_delivery_acknowledges_and_does_not_repeat_on_ok(engine, monkeypatch):
    add(engine)
    class Provider(Fake):
        async def analyze_commitments(self, context): return {'updates': []}
    engine.service.provider = Provider()
    monkeypatch.setattr('agentbridge.sales.commitments.time.time', lambda: NOW)
    ingest(engine.store, 2, text='Сали, что делать дальше?')
    await engine.service.process(-100, 10)
    row = engine.store.deliveries()[0]
    assert delivery_allowed(engine.store, row, True, NOW)
    engine.store.delivered(row['id'], 123)
    assert registry(engine)[0]['checks'] == 1
    ingest(engine.store, 3, text='Ок', respond=False)
    await engine.service.process(-100, 10)
    assert not engine.store.deliveries()


@pytest.mark.asyncio
async def test_background_failure_restart_and_normal_requests_have_priority(engine, caplog):
    ingest(engine.store, text='Завтра отправлю КП', respond=False)
    class Bad(Fake):
        async def analyze_commitments(self, context): raise RuntimeError('PRIVATE sk-secret')
    engine.service.provider = Bad()
    await engine.run_once()
    assert engine.batch(10) is not None and not registry(engine)
    assert 'PRIVATE' not in caplog.text and 'sk-secret' not in caplog.text
    class Good(Fake):
        async def analyze_commitments(self, context): return {'updates': [proposal(context['events'][0])]}
    engine.service.provider = Good()
    async with engine.service._lock:
        await engine.run_once()
    assert not registry(engine)
    engine.store.op_set('commitments_retry:-100:10', None)
    reopened = Commitments(SalesService(SalesStore(engine.store.path), Good()), -100)
    await reopened.run_once()
    assert len(registry(engine)) == 1 and reopened.batch(10) is None


@pytest.mark.asyncio
async def test_telegram_failure_does_not_acknowledge_or_send_elsewhere(engine):
    add(engine)
    engine.queue_due(10, NOW)
    adapter = SalesTelegram(-100, engine.service)
    class Bot:
        async def send_message(self, **kwargs):
            assert kwargs['chat_id'] == -100 and kwargs['message_thread_id'] == 10
            raise OSError('network')
    from unittest.mock import patch
    with patch('agentbridge.sales.commitments.time.time', return_value=NOW):
        await adapter.deliver_once(Bot())
    assert engine.store.deliveries() and registry(engine)[0]['checks'] == 0


@pytest.mark.asyncio
async def test_provider_is_ephemeral_tool_free_and_preserves_deal_thread(engine, tmp_path, monkeypatch):
    calls = []
    class Thread:
        id = 'ephemeral-commitments'
        def run(self, prompt, **kwargs):
            calls.append(json.loads(prompt))
            return NS(error=None, items=[], final_response='{"updates":[]}')
    class SDK:
        def __init__(self, config): self.config = config
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def thread_start(self, **kwargs): calls.append(kwargs); return Thread()
    monkeypatch.setattr('agentbridge.sales.provider.Codex', SDK)
    provider = SalesCodexProvider(model='fake', reasoning_effort='medium', cwd=tmp_path,
        codex_home=tmp_path / 'codex', knowledge='sales', store=engine.store)
    ingest(engine.store, text='Завтра отправлю КП', respond=False)
    engine.store.save_thread(-100, 10, 'preserved', 'original', 'first_turn')
    assert await provider.analyze_commitments(engine.batch(10)) == {'updates': []}
    assert calls[0]['ephemeral'] is True
    assert calls[0]['sandbox'].value == 'read-only'
    assert calls[0]['approval_mode'].value == 'deny_all'
    assert engine.store.thread(-100, 10)['thread_id'] == 'preserved'


def test_setting_defaults_on_and_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv('SALES_BUDDY_COMMITMENTS_ENABLED', raising=False)
    monkeypatch.delenv('TELEGRAM_SALES_BADDY_BOT_TOKEN', raising=False)
    monkeypatch.delenv('OWNER_SALES_BADDY_CHAT_ID', raising=False)
    (tmp_path / '.env').write_text('TELEGRAM_SALES_BADDY_BOT_TOKEN=fake\nOWNER_SALES_BADDY_CHAT_ID=-100\n', encoding='utf-8')
    assert SalesSettings.load(tmp_path).commitments_enabled
    monkeypatch.setenv('SALES_BUDDY_COMMITMENTS_ENABLED', 'false')
    assert not SalesSettings.load(tmp_path).commitments_enabled
    monkeypatch.delenv('SALES_BUDDY_COMMITMENTS_ENABLED')
    with (tmp_path / '.env').open('a', encoding='utf-8') as env:
        env.write('SALES_BUDDY_COMMITMENTS_ENABLED=false\n')
    assert not SalesSettings.load(tmp_path).commitments_enabled


def test_disable_cancels_pending_checks_and_preserves_registry(engine):
    add(engine)
    engine.queue_due(10, NOW)
    assert engine.store.deliveries()
    disable(engine.store)
    assert not engine.store.deliveries()
    assert len(registry(engine)) == 1 and registry(engine)[0]['checks'] == 0


def test_undated_action_is_contextual_only_and_not_asked_immediately(engine, monkeypatch):
    row = add(engine, text='Отправлю договор', due_date=None, date_basis='none')
    engine.queue_due(10, NOW)
    assert not engine.store.deliveries()
    assert engine.contextual_check(-100, 10, NOW, exclude_events=[row['event_id']]) is None
    check = engine.contextual_check(-100, 10, NOW)
    assert check['rows'][0]['id'] == row['id']
    # A contextual question must not opt an undated promise into autonomous checks.
    with engine.store.connect() as db:
        db.execute('UPDATE commitments SET next_check=?,checks=1 WHERE id=?', (NOW, row['id']))
    engine.queue_due(10, stamp('2026-10-30T11:00:00'))
    assert not engine.store.deliveries()


def test_reply_links_and_cross_topic_update_rejected(engine, monkeypatch):
    row = add(engine)
    engine.queue_due(10, NOW)
    monkeypatch.setattr('agentbridge.sales.storage.time.time', lambda: NOW)
    engine.store.delivered(engine.store.deliveries()[0]['id'], 123)
    ingest(engine.store, 2, text='Да, отправил', reply_to=123, respond=False)
    context = engine.batch(10)
    assert context['outgoing_messages'][0]['message_id'] == context['events'][0]['reply_to']
    assert json.loads(context['outgoing_messages'][0]['commitment_ids']) == [row['id']]
    add(engine, 3, topic=20)
    other = engine.batch(10)
    foreign = registry(engine)[1]
    payload = proposal(other['events'][0])
    payload.update(id=foreign['id'], state='done')
    with pytest.raises(ValueError, match='transition'):
        validate_updates({'updates': [payload]}, other)


@pytest.mark.asyncio
async def test_extract_retry_backoff_does_not_call_provider_every_minute(engine):
    ingest(engine.store, respond=False)
    class Broken(Fake):
        calls = 0
        async def analyze_commitments(self, context):
            self.calls += 1
            raise ValueError('invalid')
    engine.service.provider = Broken()
    await engine.run_once()
    await engine.run_once()
    assert engine.service.provider.calls == 1
    assert engine.batch(10) is not None


@pytest.mark.asyncio
async def test_client_draft_is_not_augmented(engine, monkeypatch):
    add(engine)
    class Draft(Fake):
        async def analyze(self, context):
            payload = await super().analyze(context)
            payload['client_draft'] = True
            return payload
        async def analyze_commitments(self, context): return {'updates': []}
    engine.service.provider = Draft()
    monkeypatch.setattr('agentbridge.sales.commitments.time.time', lambda: NOW)
    ingest(engine.store, 2, text='Сделай текст клиенту')
    await engine.service.process(-100, 10)
    assert 'Кстати' not in engine.store.deliveries()[0]['text']


@pytest.mark.asyncio
async def test_cancelled_extraction_keeps_lock_until_sdk_worker_finishes(engine, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    provider = SalesCodexProvider(model='fake', reasoning_effort='medium', cwd=engine.store.path.parent,
        codex_home=engine.store.path.parent / 'codex', knowledge='sales', store=engine.store)
    def slow(context):
        entered.set()
        release.wait(3)
        return {'updates': []}
    monkeypatch.setattr(provider, '_commitments', slow)
    engine.service.provider = provider
    ingest(engine.store, respond=False)
    task = asyncio.create_task(engine.run_once())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(.02)
        assert engine.service._lock.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not engine.service._lock.locked()
    assert engine.batch(10) is not None


@pytest.mark.asyncio
async def test_long_document_reuses_full_chunk_pipeline_and_cache(engine, tmp_path, monkeypatch):
    contexts = []
    class Thread:
        id = 'ephemeral'
        def run(self, prompt, **kwargs):
            context = json.loads(prompt)
            contexts.append(context)
            if kwargs['output_schema'] == SCHEMA:
                return NS(error=None, items=[], final_response='{"updates":[]}')
            return NS(error=None, items=[], final_response=json.dumps(dict(answer='Источник прочитан.', facts=[],
                basis=[], goal='', next_action='')))
    class SDK:
        def __init__(self, config): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def thread_start(self, **kwargs):
            assert kwargs['ephemeral']
            return Thread()
    monkeypatch.setattr('agentbridge.sales.provider.Codex', SDK)
    provider = SalesCodexProvider(model='fake', reasoning_effort='medium', cwd=tmp_path,
        codex_home=tmp_path / 'codex', knowledge='sales', store=engine.store)
    original = 'НАЧАЛО\n' + ('исходный разговор\n' * 11000) + 'КОНЕЦ'
    ingest(engine.store, text=original, source='text_document', respond=False)
    batch = engine.batch(10)
    assert await provider.analyze_commitments(batch) == {'updates': []}
    parts = [c['events'][0]['text'] for c in contexts if 'document_part' in c]
    assert ''.join(parts) == original
    assert contexts[-1]['events'][0]['fully_read']
    previous_count = len(contexts)
    assert await provider.analyze_commitments(batch) == {'updates': []}
    assert len(contexts) == previous_count + 1  # Only the final extraction; document parts are cached.


def test_denied_media_does_not_block_authorized_commitments(engine):
    add(engine)
    ingest(engine.store, 2, source='audio_pending', text='', authorization_pending=True)
    engine.store.authorize(2, False, '')
    engine.queue_due(10, NOW)
    assert engine.store.deliveries()


def test_terminal_media_failure_holds_old_promises_but_allows_fresh_updates(engine):
    row = add(engine)
    ingest(engine.store, 2, source='audio_pending', text='', respond=False)
    with engine.store.connect() as db:
        db.execute('UPDATE events SET processed=3 WHERE id=2')
    engine.queue_due(10, NOW)
    assert not engine.store.deliveries()
    assert engine.batch(10) is None
    assert registry(engine)[0]['quiet']
    update(engine, row['id'], 3, 'done', 'КП отправил')
    assert registry(engine)[0]['state'] == 'done'


def reminder(engine, number=1, **changes):
    return add(engine, number, text='Сали, напомни завтра в 15:00 отправить КП',
               due_date=None, reminder='set', remind_at='2026-10-12T15:00:00+07:00', **changes)


def clear_notices(engine):
    for row in engine.store.deliveries():
        if row['delivery_key'].startswith('reminder-notice:'):
            engine.store.delivered(row['id'], row['id'] + 100)


def test_explicit_reminder_reuses_promise_and_suppresses_both_checks(engine, monkeypatch):
    add(engine)
    reminder(engine, 2, now=stamp('2026-10-11T17:00:00'))
    assert len(registry(engine)) == 1
    assert '12.10.2026 в 15:00' in engine.store.deliveries()[0]['text']
    clear_notices(engine)
    engine.queue_due(10, stamp('2026-10-12T11:00:00'))
    assert not engine.store.deliveries()
    assert engine.contextual_check(-100, 10, stamp('2026-10-12T11:00:00')) is None
    reopened = Commitments(SalesService(SalesStore(engine.store.path), Fake()), -100)
    reopened.queue_reminders(stamp('2026-10-12T14:59:00'))
    assert not engine.store.deliveries()
    at = stamp('2026-10-12T15:00:00')
    reopened.queue_reminders(at)
    reopened.queue_reminders(at)
    row, = engine.store.deliveries()
    assert 'tg://user?id=7' in row['text']
    assert delivery_allowed(engine.store, row, True, at)
    monkeypatch.setattr('agentbridge.sales.storage.time.time', lambda: at)
    engine.store.delivered(row['id'], 200)
    assert registry(engine)[0]['reminder_sent_at'] == at
    assert registry(engine)[0]['checks'] == 0
    assert registry(engine)[0]['next_check'] == stamp('2026-10-15T10:00:00')
    assert engine.contextual_check(-100, 10, at) is None
    reopened.queue_reminders(at + 60)
    assert not engine.store.deliveries()


@pytest.mark.parametrize('state,intent', [('done', 'keep'), ('cancelled', 'keep'), ('paused', 'keep'), ('active', 'cancel')])
def test_explicit_reminder_completion_or_cancellation_cancels_queued_send(engine, state, intent):
    row = add(engine, text='Сали, напомни 16.10.2026 в 15:00 отправить КП', due_date=None,
              reminder='set', remind_at='2026-10-16T15:00:00+07:00')
    clear_notices(engine)
    engine.queue_reminders(stamp('2026-10-16T15:00:00'))
    delivery, = engine.store.deliveries()
    update(engine, row['id'], 2, state, 'КП отправил, напоминание отмени', reminder=intent)
    assert not delivery_allowed(engine.store, delivery, True, stamp('2026-10-16T15:00:00'))
    assert registry(engine)[0]['remind_at'] is None


def test_explicit_reminder_postpone_and_clarification(engine):
    row = add(engine, text='Сали, напомни отправить КП', due_date=None, reminder='clarify')
    assert 'На какую дату' in engine.store.deliveries()[0]['text']
    assert registry(engine)[0]['quiet']
    update(engine, row['id'], 2, 'active', '16.10.2026 в 15:00', reminder='set',
           remind_at='2026-10-16T15:00:00+07:00')
    assert not registry(engine)[0]['quiet']
    clear_notices(engine)
    engine.queue_reminders(stamp('2026-10-16T15:00:00'))
    old, = engine.store.deliveries()
    update(engine, row['id'], 3, 'active', 'Перенеси на 17.10.2026 в 21:30', reminder='set',
           remind_at='2026-10-17T21:30:00+07:00')
    assert not delivery_allowed(engine.store, old, True, stamp('2026-10-16T15:00:00'))
    clear_notices(engine)
    engine.queue_reminders(stamp('2026-10-17T21:30:00'))
    new, = engine.store.deliveries()
    assert delivery_allowed(engine.store, new, True, stamp('2026-10-17T21:30:00'))


@pytest.mark.parametrize('change', [
    {'remind_at': '2026-10-16T15:00:00'},
    {'remind_at': '2026-10-16T16:00:00+07:00'},
    {'remind_at': '2026-10-17T15:00:00+07:00'},
    {'source': 'text_document'},
    {'text': 'Завтра отправлю КП'},
])
def test_explicit_reminder_rejects_invented_time_or_non_request(engine, change):
    source = change.get('source', 'telegram_text')
    ingest(engine.store, text=change.get('text', 'Сали, напомни 16.10.2026 в 15:00 отправить КП'),
           source=source, respond=False)
    batch = engine.batch(10)
    row = proposal(batch['events'][0])
    row.update(reminder='set', due_date=None, remind_at=change.get('remind_at', '2026-10-16T15:00:00+07:00'))
    with pytest.raises(ValueError):
        engine.apply(batch, {'updates': [row]}, NOW)
    assert not registry(engine)
    assert engine.batch(10)


def test_expired_request_asks_for_time_instead_of_claiming_scheduled(engine):
    reminder(engine)
    assert registry(engine)[0]['remind_at'] is None
    assert 'На какую дату' in engine.store.deliveries()[0]['text']


@pytest.mark.asyncio
async def test_explicit_reminder_delivers_without_codex_and_retries_telegram(engine, monkeypatch):
    add(engine, text='Сали, напомни 17.10.2026 в 21:30 отправить <КП>', action='Отправить <КП>',
        due_date=None, reminder='set', remind_at='2026-10-17T21:30:00+07:00')
    clear_notices(engine)
    at = stamp('2026-10-17T21:30:00')
    monkeypatch.setattr('agentbridge.sales.commitments.time.time', lambda: at)
    engine.store.op_set('codex_failure', {'kind': 'codex_usage_limit'})
    await engine.run_once()
    adapter = SalesTelegram(-100, engine.service)
    class Bot:
        failed = True
        async def send_message(self, **kwargs):
            assert kwargs['chat_id'] == -100 and kwargs['message_thread_id'] == 10
            assert kwargs['parse_mode'] == 'HTML'
            assert '&lt;КП&gt;' in kwargs['text']
            if self.failed:
                raise OSError('network')
            return NS(message_id=99)
    bot = Bot()
    await adapter.deliver_once(bot)
    assert registry(engine)[0]['reminder_sent_at'] is None
    bot.failed = False
    await adapter.deliver_once(bot)
    assert registry(engine)[0]['reminder_sent_at'] == at
    assert not engine.store.deliveries()


def test_reminder_relative_time_uses_source_timestamp_and_topic_isolation(engine):
    add(engine, topic=20, text='Сали, напомни через 30 минут отправить КП', due_date=None,
        reminder='set', remind_at='2026-10-11T10:30:00+00:00', now=stamp('2026-10-11T17:00:00'))
    clear_notices(engine)
    engine.queue_reminders(stamp('2026-10-11T17:30:00'))
    delivery, = engine.store.deliveries()
    assert delivery['topic_id'] == 20
    assert not engine.context(-100, 10)
    assert not delivery_allowed(engine.store, delivery, False, stamp('2026-10-11T17:30:00'))
    assert registry(engine)[0]['remind_at'] == stamp('2026-10-11T17:30:00')


def test_legacy_commitment_migration_preserves_thread_and_registry(engine):
    old = add(engine)
    engine.store.save_thread(-100, 10, 'keep-thread', 'keep-version', 'first_turn')
    with engine.store.connect() as db:
        for name in ('remind_at', 'reminder_event_id', 'reminder_sent_at'):
            db.execute(f'ALTER TABLE commitments DROP COLUMN {name}')
    reopened = SalesStore(engine.store.path)
    assert reopened.thread(-100, 10)['thread_id'] == 'keep-thread'
    with reopened.connect() as db:
        row = dict(db.execute('SELECT * FROM commitments WHERE id=?', (old['id'],)).fetchone())
    assert row['quote'] == old['quote'] and row['next_check'] == old['next_check']
    assert row['remind_at'] is None


def test_acknowledgement_of_old_reminder_cannot_consume_rescheduled_one(engine, monkeypatch):
    row = add(engine, text='Сали, напомни 16.10.2026 в 15:00 отправить КП', due_date=None,
              reminder='set', remind_at='2026-10-16T15:00:00+07:00')
    clear_notices(engine)
    engine.queue_reminders(stamp('2026-10-16T15:00:00'))
    delivery, = engine.store.deliveries()
    # Telegram has accepted the old message while a new time is being persisted.
    update(engine, row['id'], 2, 'active', 'Перенеси на 17.10.2026 в 16:30', reminder='set',
           remind_at='2026-10-17T16:30:00+07:00')
    monkeypatch.setattr('agentbridge.sales.storage.time.time', lambda: stamp('2026-10-16T15:00:00'))
    engine.store.delivered(delivery['id'], 999)
    assert registry(engine)[0]['reminder_sent_at'] is None
    assert registry(engine)[0]['remind_at'] == stamp('2026-10-17T16:30:00')
