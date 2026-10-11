import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from openai_codex import ApprovalMode, Sandbox
from telegram.ext import CallbackQueryHandler

from agentbridge.agents.codex import CodexProvider, _SELF_AUDIT_SCHEMA, _log_tool_metadata, validate_structured_output_schema
from agentbridge.application import AgentBridgeApplication
from agentbridge.self_audit import SelfAudit, collect_summary, validate_proposals
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.telegram.bot import create_telegram_application, due_report_windows


def queue(store, date='2026-10-10', delivered=True):
    delivery = store.queue_daily_report(date, '2026-10-10T00:00:00+00:00', '2026-10-11T00:00:00+00:00')
    if delivered:
        with store._connect() as conn:
            conn.execute('UPDATE owner_query_deliveries SET owner_message_id=100 WHERE id=?', (delivery,))
    return delivery


def summary(count=2):
    return {'window': {'start_utc': '2026-10-10T00:00:00+00:00', 'end_utc': '2026-10-11T00:00:00+00:00'},
            'evidence': {'logs:codex_turn_failed': {'source': 'logs', 'event': 'codex_turn_failed',
                         'current_count': count, 'previous_7d_count': 3}}}


def payload():
    return {'proposals': [{'issue': 'logs:codex_turn_failed', 'proposal': 'Проверять транспорт перед повтором.',
                          'cause': 'Вероятно, обрыв соединения.', 'benefit': 'Меньше лишних повторов.',
                          'risk': 'Можно пропустить допустимый повтор.', 'priority': 'P2', 'uncertain': True}]}


def test_schedule_and_report_delivery_gate_and_restart(tmp_path):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    now = datetime(2026, 10, 11, 4, 30, tzinfo=timezone.utc)
    windows = due_report_windows(now, '2026-10-09', '11:30', 'Asia/Novosibirsk')
    assert windows[0][0] == '2026-10-10'
    assert due_report_windows(now - timedelta(seconds=1), '2026-10-09', '11:30', 'Asia/Novosibirsk') == []
    delivery = queue(store, delivered=False)
    assert store.claim_self_audit() is None
    assert queue(store, delivered=False) == delivery
    queue(store)
    first = store.claim_self_audit()
    assert first['report_date'] == '2026-10-10'
    assert store.claim_self_audit() is None
    restarted = ChatThreadStore(store.database_path)
    restarted.recover_self_audits()
    assert restarted.claim_self_audit()['report_date'] == first['report_date']
    restarted.finish_self_audit(first['report_date'], [])
    restarted.recover_self_audits()
    assert restarted.claim_self_audit() is None


def test_missed_audits_coalesce_without_losing_reports(tmp_path):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    for date in ('2026-10-08', '2026-10-09', '2026-10-10'):
        queue(store, date)
    assert store.claim_self_audit()['report_date'] == '2026-10-10'
    assert store.claim_self_audit() is None
    with store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM daily_reports').fetchone()[0] == 3


def test_decisions_dedup_rejection_threshold_and_later(tmp_path):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    queue(store)
    store.claim_self_audit()
    store.finish_self_audit('2026-10-10', validate_proposals(payload(), summary()))
    proposal = store.pending_self_audit_proposals()[0]
    store.record_self_audit_delivery(proposal['id'], 5)
    assert not store.decide_self_audit(proposal['id'], 'accepted', user_id=77, message_id=6)
    assert store.decide_self_audit(proposal['id'], 'later', user_id=77, message_id=5)
    assert not store.decide_self_audit(proposal['id'], 'later', user_id=77, message_id=5)
    assert store.decide_self_audit(proposal['id'], 'rejected', user_id=77, message_id=5)
    assert not store.decide_self_audit(proposal['id'], 'accepted', user_id=77, message_id=5)
    for date, count in (('2026-10-11', 3), ('2026-10-12', 5)):
        queue(store, date)
        store.claim_self_audit()
        store.finish_self_audit(date, validate_proposals(payload(), summary(count)))
        store.finish_self_audit(date, validate_proposals(payload(), summary(count)))
    assert len(store.pending_self_audit_proposals()) == 1
    new = store.pending_self_audit_proposals()[0]
    store.record_self_audit_delivery(new['id'], 7)
    store.decide_self_audit(new['id'], 'accepted', user_id=77, message_id=7)
    queue(store, '2026-10-13')
    store.claim_self_audit()
    store.finish_self_audit('2026-10-13', validate_proposals(payload(), summary(50)))
    assert store.pending_self_audit_proposals() == []


def test_collector_24h_baseline_partial_logs_and_secrets(tmp_path):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    secret = 'sk-proj-abcdefghijklmnopqrstuvwxyz'
    lines = [
        '2026-10-02T01:00:00+00:00 event=codex_turn_failed',  # outside eight days
        '2026-10-04T01:00:00+00:00 event=codex_turn_failed',
        '2026-10-10T01:00:00+00:00 event=codex_turn_failed thread_id=private-thread',
        '2026-10-10T02:00:00+00:00 event=codex_turn_finished thread_id=private-thread',
        '2026-10-10T03:00:00+00:00 event=codex_tool_completed tool_type=commandExecution status=failed',
        '2026-10-10T04:00:00+00:00 event=codex_turn_started request_key=' + 'a' * 64,
        '2026-10-10T05:00:00+00:00 event=codex_turn_started request_key=' + 'a' * 64,
        '2026-10-11T00:00:00+00:00 event=codex_turn_failed',  # exclusive end
    ]
    (tmp_path / 'agentbridge.log').write_text('\n'.join('timestamp=' + line + ' prompt=CLIENT_TEXT token=' + secret for line in lines), encoding='utf-8')
    with store._connect() as conn:
        conn.execute("INSERT INTO operational_events VALUES(NULL, 'codex_turn_failed', 'ERROR', '2026-10-10T01:00:00+00:00')")
    result = collect_summary(store, tmp_path, '2026-10-11T00:00:00+00:00')
    evidence = result['evidence']['logs:codex_turn_failed']
    assert evidence['current_count'] == 1 and evidence['previous_7d_count'] == 1
    assert result['evidence']['sqlite.operational_events:codex_turn_failed']['current_count'] == 1
    assert result['recovery_indicators']['failed_turn_then_success_same_thread'] == 1
    assert result['tool_counts']['current:commandExecution']['failed'] == 1
    assert result['evidence']['logs:codex_request_repeated']['current_count'] == 1
    encoded = json.dumps(result)
    assert all(value not in encoded for value in (secret, 'CLIENT_TEXT', 'private-thread', 'a' * 64))
    assert result['log_coverage']['first_utc'] == '2026-10-04T01:00:00+00:00'


def test_tool_metadata_drops_commands_arguments_outputs(caplog):
    caplog.set_level('INFO')
    sensitive = 'sk-proj-abcdefghijklmnopqrstuvwxyz CLIENT_TEXT'
    _log_tool_metadata([SimpleNamespace(type='commandExecution', status='completed', exit_code=1,
                                        command=sensitive, aggregated_output=sensitive, duration_ms=20),
                        SimpleNamespace(type='commandExecution', status='completed', exit_code=0, duration_ms=10),
                        SimpleNamespace(type='mcpToolCall', status='failed', arguments=sensitive, result=sensitive)])
    assert sensitive not in caplog.text
    assert 'status=failed' in caplog.text and 'codex_tool_recovered' in caplog.text


def test_prompt_fingerprint_is_anonymous_and_changes_on_restart(caplog, monkeypatch):
    caplog.set_level('INFO')
    class Thread:
        id = 'fake-thread'
        def run(self, *args, **kwargs):
            return SimpleNamespace(error=None, final_response='{"proposals": []}', items=[])
    provider = CodexProvider()
    prompt = 'CLIENT_PRIVATE_TEXT sk-proj-abcdefghijklmnopqrstuvwxyz'
    for text in (prompt, prompt, prompt + ' different'):
        provider._run_json(Thread(), text, _SELF_AUDIT_SCHEMA)
    monkeypatch.setattr('agentbridge.agents.codex._TELEMETRY_KEY', b'x' * 32)
    provider._run_json(Thread(), prompt, _SELF_AUDIT_SCHEMA)
    keys = [record.getMessage().split('request_key=')[1] for record in caplog.records if 'request_key=' in record.getMessage()]
    assert keys[0] == keys[1] and keys[0] != keys[2] and keys[0] != keys[3]
    assert 'CLIENT_PRIVATE_TEXT' not in caplog.text and 'sk-proj-' not in caplog.text


def test_rotated_logs_are_read_before_current_log(tmp_path):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    key = 'b' * 64
    (tmp_path / 'agentbridge.log.2026-10-09').write_text(
        f'timestamp=2026-10-09T23:00:00+00:00 event=codex_turn_started request_key={key}\n'
        'timestamp=2026-10-09T23:01:00+00:00 event=codex_turn_failed thread_id=same\n', encoding='utf-8')
    (tmp_path / 'agentbridge.log').write_text(
        f'timestamp=2026-10-10T01:00:00+00:00 event=codex_turn_started request_key={key}\n'
        'timestamp=2026-10-10T01:01:00+00:00 event=codex_turn_finished thread_id=same\n', encoding='utf-8')
    facts = collect_summary(store, tmp_path, '2026-10-11T00:00:00+00:00')
    assert facts['evidence']['logs:codex_request_repeated']['current_count'] == 1
    assert facts['recovery_indicators']['failed_turn_then_success_same_thread'] == 1


@pytest.mark.parametrize('mutation', ['secret', 'unknown_source', 'too_many', 'zero_count'])
def test_invalid_model_evidence_and_secret_rejected(mutation):
    data, facts = payload(), summary()
    if mutation == 'secret':
        data['proposals'][0]['cause'] = 'sk-proj-abcdefghijklmnopqrstuvwxyz'
    elif mutation == 'unknown_source':
        data['proposals'][0]['issue'] = 'sessions:other-project'
    elif mutation == 'too_many':
        data['proposals'] *= 4
    else:
        facts = summary(0)
    with pytest.raises(ValueError):
        validate_proposals(data, facts)
    assert validate_proposals({'proposals': []}, facts) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['empty', 'failure', 'limit'])
async def test_empty_failure_limit_no_retry_or_automatic_changes(tmp_path, outcome):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    queue(store)
    calls = []
    if outcome == 'limit':
        store.claim_codex_usage_limit('', 'limit', source_timezone_name='Europe/Moscow', local_hint='')
    class Provider:
        async def analyze_self_audit(self, **kwargs):
            calls.append(kwargs)
            if outcome == 'failure':
                raise RuntimeError('sk-proj-abcdefghijklmnopqrstuvwxyz')
            return {'proposals': []}
    context = tmp_path / 'working_context.md'
    context.write_text('unchanged', encoding='utf-8')
    audit = SelfAudit(store, Provider(), tmp_path)
    audit.schedule()
    first_task = audit.task
    audit.schedule()
    assert audit.task is first_task
    await audit.task
    audit.schedule()
    await audit.task
    assert len(calls) == (0 if outcome == 'limit' else 1)
    assert store.pending_self_audit_proposals() == []
    assert context.read_text(encoding='utf-8') == 'unchanged'
    with store._connect() as conn:
        for table in ('owner_general_tasks', 'memory_entries', 'memory_drafts', 'chat_threads', 'owner_query_threads'):
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_provider_is_ephemeral_read_only_and_no_working_context(tmp_path, monkeypatch):
    starts, configs, turns = [], [], []
    class Thread:
        id = 'isolated'
        def run(self, prompt, **kwargs):
            turns.append((prompt, kwargs))
            return SimpleNamespace(error=None, final_response='{"proposals": []}', items=[])
    class Codex:
        def __init__(self, config): configs.append(config)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def thread_start(self, **kwargs):
            starts.append(kwargs)
            return Thread()
    monkeypatch.setattr('agentbridge.agents.codex.Codex', Codex)
    def forbidden(*args, **kwargs):
        raise AssertionError('Audit must not launch a write/developer command')
    monkeypatch.setattr('agentbridge.agents.codex.subprocess.run', forbidden)
    context = tmp_path / 'working_context.md'
    context.write_text('PRIVATE_CONTEXT', encoding='utf-8')
    provider = CodexProvider(cwd=tmp_path, owner_context_path=context)
    assert await provider.analyze_self_audit(summary=summary()) == {'proposals': []}
    assert starts[0]['ephemeral'] is True
    assert starts[0]['sandbox'] == turns[0][1]['sandbox'] == Sandbox.read_only
    assert starts[0]['approval_mode'] == turns[0][1]['approval_mode'] == ApprovalMode.deny_all
    assert 'PRIVATE_CONTEXT' not in str(starts) + str(turns)
    assert 'mcp_servers={}' in configs[0].config_overrides
    assert 'features.shell_tool=false' in configs[0].config_overrides
    assert 'features.hooks=false' in configs[0].config_overrides
    validate_structured_output_schema(_SELF_AUDIT_SCHEMA)


@pytest.mark.asyncio
@pytest.mark.parametrize('chat_id,user_id,message_id,configured,expected', [
    (77, 77, 5, None, True), (77, 88, 5, None, False), (88, 77, 5, None, False),
    (77, 77, 6, None, False), (-100, 77, 5, None, False), (-100, 77, 5, 77, True),
    (-100, 88, 5, 77, False),
])
async def test_callbacks_authorization_and_no_developer_or_memory_flow(tmp_path, chat_id, user_id, message_id, configured, expected):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    queue(store)
    store.claim_self_audit()
    store.finish_self_audit('2026-10-10', validate_proposals(payload(), summary()))
    store.record_self_audit_delivery(1, 5)
    class Query:
        data = 'audit:accepted:1'
        message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)
        async def answer(self): pass
        async def edit_message_reply_markup(self, **kwargs): pass
    class Bot:
        async def send_message(self, **kwargs): return SimpleNamespace(message_id=8)
    audit = SelfAudit(store, SimpleNamespace(), tmp_path)
    app = create_telegram_application(token='fake', owner_chat_id=77 if chat_id > 0 else -100,
        message_service=SimpleNamespace(store=store), self_audit=audit, self_audit_owner_user_id=configured)
    handler = next(h for h in app.handlers[0] if isinstance(h, CallbackQueryHandler))
    update = SimpleNamespace(callback_query=Query(), update_id=123,
                             effective_user=SimpleNamespace(id=user_id, is_bot=False))
    assert handler.pattern.match(Query.data)
    await handler.callback(update, SimpleNamespace(bot=Bot()))
    await handler.callback(update, SimpleNamespace(bot=Bot()))
    with store._connect() as conn:
        assert conn.execute('SELECT status FROM self_audit_proposals').fetchone()[0] == ('accepted' if expected else 'pending')
        assert conn.execute('SELECT count(*) FROM owner_general_tasks').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM memory_entries').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_report_sent_before_slow_audit_and_telegram_remains_free(tmp_path, chat_registry):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    service = AgentBridgeApplication(chat_registry, store, SimpleNamespace(), owner_chat_id=77)
    report_id = queue(store, delivered=False)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    class Provider:
        async def analyze_self_audit(self, **kwargs):
            assert sent and 'Отчёт' in sent[0]['text']
            entered.set()
            await release.wait()
            return {'proposals': []}
    class Bot:
        async def set_my_commands(self, *args, **kwargs): pass
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(message_id=len(sent))
    audit = SelfAudit(store, Provider(), tmp_path)
    app = create_telegram_application(token='fake', owner_chat_id=77, message_service=service,
        self_audit=audit, daily_report_enabled=True, catchup_idle_seconds=0, delivery_retry_seconds=0.01)
    app.bot = Bot()
    app._running = True
    await app.post_init(app)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with store._connect() as conn:
            assert conn.execute('SELECT owner_message_id FROM owner_query_deliveries WHERE id=?', (report_id,)).fetchone()[0]
        service.store.queue_operational_notice('while-audit-runs', 'Independent delivery')
        for _ in range(100):
            if any(message['text'] == 'Independent delivery' for message in sent): break
            await asyncio.sleep(0.01)
        assert any(message['text'] == 'Independent delivery' for message in sent)
    finally:
        release.set()
        await app.post_stop(app)
    assert all(message['chat_id'] == 77 for message in sent)


@pytest.mark.asyncio
async def test_cancelled_audit_restores_after_restart(tmp_path, monkeypatch):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    queue(store)
    entered = asyncio.Event()
    class Slow:
        async def analyze_self_audit(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()
    audit = SelfAudit(store, Slow(), tmp_path)
    audit.schedule()
    await asyncio.wait_for(entered.wait(), 2)
    await audit.stop()
    with store._connect() as conn:
        assert conn.execute('SELECT status FROM self_audit_runs').fetchone()[0] == 'running'
    restarted = ChatThreadStore(store.database_path)
    restarted.recover_self_audits()
    monkeypatch.setattr('agentbridge.self_audit.collect_summary', lambda *args: summary())
    class Ready:
        async def analyze_self_audit(self, **kwargs): return payload()
    resumed = SelfAudit(restarted, Ready(), tmp_path)
    resumed.schedule()
    await resumed.task
    assert len(restarted.pending_self_audit_proposals()) == 1
    resumed.schedule()
    await resumed.task
    assert len(restarted.pending_self_audit_proposals()) == 1


@pytest.mark.asyncio
async def test_saved_card_retries_delivery_after_restart(tmp_path, chat_registry):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    queue(store)
    store.claim_self_audit()
    store.finish_self_audit('2026-10-10', validate_proposals(payload(), summary()))
    calls = []
    class Bot:
        async def set_my_commands(self, *args, **kwargs): pass
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError('fake Telegram failure')
            return SimpleNamespace(message_id=len(calls))
    for _ in range(2):
        current = ChatThreadStore(store.database_path)
        audit = SelfAudit(current, SimpleNamespace(), tmp_path)
        service = AgentBridgeApplication(chat_registry, current, SimpleNamespace(), owner_chat_id=77)
        app = create_telegram_application(token='fake', owner_chat_id=77, message_service=service,
            self_audit=audit, daily_report_enabled=True, catchup_idle_seconds=0, delivery_retry_seconds=0.01)
        app.bot, app._running = Bot(), True
        await app.post_init(app)
        for _ in range(100):
            if not current.pending_self_audit_proposals(): break
            await asyncio.sleep(0.01)
        await app.post_stop(app)
        assert not current.pending_self_audit_proposals()
    cards = [call for call in calls if 'reply_markup' in call]
    assert len(cards) == 2  # failed attempt, then one successful delivery
    assert [b.text for b in cards[-1]['reply_markup'].inline_keyboard[0]] == ['Принять', 'Отклонить', 'Позже']
    assert all(call['chat_id'] == 77 for call in calls)


def test_new_failure_category_is_material_evidence(tmp_path):
    store = ChatThreadStore(tmp_path / 'db.sqlite3')
    queue(store)
    store.claim_self_audit()
    store.finish_self_audit('2026-10-10', validate_proposals(payload(), summary(2)))
    store.record_self_audit_delivery(1, 5)
    store.decide_self_audit(1, 'rejected', user_id=77, message_id=5)
    queue(store, '2026-10-11')
    store.claim_self_audit()
    facts = summary(2)
    facts['evidence']['logs:codex_turn_failed']['failure_kinds'] = {'codex_auth': 1}
    store.finish_self_audit('2026-10-11', validate_proposals(payload(), facts))
    assert len(store.pending_self_audit_proposals()) == 1
