from datetime import datetime, timezone
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agentbridge.sales.bot import SalesTelegram, create_sales_application
from agentbridge.sales.provider import SalesCodexProvider, SCHEMA
from agentbridge.sales.service import SalesService, validate_result
from agentbridge.sales.settings import SalesSettings
from agentbridge.sales.storage import SalesStore
from agentbridge.agents.codex import validate_structured_output_schema
from openai_codex import ApprovalMode, Sandbox


def ingest(store, update=1, topic=10, text='Клиент Птицефабрика выбирает маркировку', **kwargs):
    return store.ingest(update_id=update, chat_id=-100, topic_id=topic, title='Птицефабрика',
        message_id=update, author_id=7, author='Сергей', date='2026-10-11T10:00:00+00:00',
        text=text, **kwargs)


def result(context, replaces=0):
    event = context['events'][-1]
    return dict(answer='Предложите встречу, чтобы уточнить критерии выбора.',
        next_action='Предложить встречу', goal='Получить согласие клиента', basis=[event['id']],
        facts=[dict(field='confirmed', text=event['text'], kind='fact',
                    event_id=event['id'], quote=event['text'], replaces=replaces)])


class Fake:
    def __init__(self):
        self.contexts = []

    async def analyze(self, context):
        self.contexts.append(context)
        return result(context)


@pytest.mark.asyncio
async def test_vertical_slice_history_isolation_restart_and_feedback(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    fake = Fake()
    service = SalesService(store, fake)
    assert ingest(store)
    assert not ingest(store)
    await service.process(-100, 10)
    assert not store.pending_topics()
    assert store.context(-100, 10)['facts'][0]['text'].startswith('Клиент Птицефабрика')
    delivery = store.deliveries()[0]
    store.delivered(delivery['id'], 555)
    # Startup reopens SQLite; no model threads are required to reconstruct the card.
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, 2, text='Клиент готов рассмотреть обследование, спрашивает, входит ли в стоимость', reply_to=555)
    ingest(store, 3, topic=20, text='Молочный завод выбирает весы')
    service = SalesService(store, fake)
    await service.process(-100, 10)
    second = fake.contexts[-1]
    assert second['history'][0]['text'].startswith('Клиент Птицефабрика')
    assert second['events'][0]['source'] == 'manager_feedback'
    assert second['events'][0]['reply_to'] == 555
    assert second['previous_recommendations'][0]['goal'] == 'Получить согласие клиента'
    assert 'Молочный' not in json.dumps(second, ensure_ascii=False)
    await service.process(-100, 20)
    assert 'Птицефабрика выбирает' not in json.dumps(fake.contexts[-1], ensure_ascii=False)
    assert len(store.context(-100, 10)['facts']) == 2
    assert len(store.context(-100, 20)['facts']) == 1
    await service.process(-100, 10)
    assert len(fake.contexts) == 3


@pytest.mark.asyncio
async def test_model_error_and_invalid_patch_preserve_history_and_card(tmp_path, caplog):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store)
    class Bad:
        async def analyze(self, context):
            raise RuntimeError('PRIVATE PROMPT sk-testverysecret1234567890')
    await SalesService(store, Bad()).process(-100, 10)
    assert 'PRIVATE' not in caplog.text and 'secret' not in caplog.text
    context = store.context(-100, 10)
    assert len(context['events']) == 1 and not context['facts']
    await SalesService(store, Bad()).process(-100, 10)
    assert len(store.deliveries()) == 1
    restarted = SalesStore(store.path)
    with restarted.connect() as db:
        db.execute('UPDATE deals SET retry_at=0')
    await SalesService(restarted, Fake()).process(-100, 10)
    assert restarted.context(-100, 10)['facts']
    assert not restarted.pending_topics()


@pytest.mark.asyncio
async def test_correction_preserves_old_fact_and_source(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, text='Клиент выбирает 10 весов')
    await SalesService(store, Fake()).process(-100, 10)
    ingest(store, 2, text='Исправление: клиент выбирает 12 весов')
    context = store.context(-100, 10)
    patch = validate_result(result(context, replaces=context['facts'][0]['id']), context)
    assert store.complete(-100, 10, context['events'], patch)
    assert store.context(-100, 10)['facts'][0]['text'].endswith('12 весов')
    with store.connect() as db:
        rows = db.execute('SELECT * FROM facts ORDER BY id').fetchall()
        assert rows[0]['superseded_by'] == rows[1]['id']
        assert rows[0]['quote'] == 'Клиент выбирает 10 весов'
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 2
    assert not store.complete(-100, 10, context['events'], patch)


@pytest.mark.parametrize('mutation', ['unknown', 'source', 'invented_quote', 'replace', 'no_basis', 'downgrade'])
def test_untrusted_output_validation(tmp_path, mutation):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store)
    first = store.context(-100, 10)
    store.complete(-100, 10, first['events'], validate_result(result(first), first))
    ingest(store, 2, text='Новый звонок по сделке')
    context = store.context(-100, 10)
    payload = result(context)
    fact = payload['facts'][0]
    if mutation == 'unknown': fact['field'] = 'unknowns'
    if mutation == 'source': fact['event_id'] = 999
    if mutation == 'invented_quote': fact['quote'] = 'Бесплатное обследование подтверждено'
    if mutation == 'replace': fact['replaces'] = context['facts'][0]['id']
    if mutation == 'no_basis': payload['basis'] = []
    if mutation == 'downgrade':
        fact['replaces'] = context['facts'][0]['id']
        fact['kind'] = 'assumption'
        context['events'][0]['text'] += ' Уточнение'
    with pytest.raises(ValueError):
        validate_result(payload, context)
    assert len(store.context(-100, 10)['facts']) == 1
    assert len(store.context(-100, 10)['events']) == 1


def test_unknown_and_assumption_remain_labelled_and_dedup_facts(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, text='Не знаем бюджет')
    context = store.context(-100, 10)
    payload = result(context)
    with pytest.raises(ValueError):
        validate_result(payload, context)
    payload['facts'][0].update(field='unknowns', kind='unknown')
    store.complete(-100, 10, context['events'], validate_result(payload, context))
    ingest(store, 2, text='Не знаем бюджет')
    context = store.context(-100, 10)
    payload = result(context)
    payload['facts'][0].update(field='unknowns', kind='unknown')
    assert validate_result(payload, context)['facts'] == []
    assert context['facts'][0]['kind'] == 'unknown'


def update(chat_id=-100, topic=10, user_id=7, text='Предыстория сделки', **overrides):
    user = NS(id=user_id, is_bot=False, full_name='Сергей')
    chat = NS(id=chat_id, type='supergroup', is_forum=True)
    message = NS(message_id=1, message_thread_id=topic, is_topic_message=True,
        text=text, caption=None, date=datetime.now(timezone.utc), reply_to_message=None)
    for key, value in overrides.items():
        if key in ('is_bot',): setattr(user, key, value)
        elif key in ('type', 'is_forum'): setattr(chat, key, value)
        else: setattr(message, key, value)
    return NS(update_id=1, effective_message=message, effective_user=user, effective_chat=chat)


class Bot:
    id, username = 123, 'sali_bot'
    def __init__(self, status='member'):
        self.status, self.sent = status, []
    async def get_chat_member(self, chat_id, user_id):
        return NS(status=self.status, is_member=self.status != 'restricted')
    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return NS(message_id=len(self.sent) + 100)


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs,status,accepted', [
    ({}, 'member', True), ({'user_id': 99}, 'member', True),
    ({}, 'administrator', True), ({}, 'creator', True),
    ({'chat_id': -200}, 'member', False), ({'topic': None}, 'member', False),
    ({'is_bot': True}, 'member', False), ({'sender_chat': NS(id=-100)}, 'member', False),
    ({'is_forum': False}, 'member', False), ({}, 'left', False),
    ({}, 'restricted', False),
])
async def test_authorization_group_members_only(tmp_path, kwargs, status, accepted):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    adapter = SalesTelegram(-100, SalesService(store, Fake()))
    await adapter.ingest(update(**kwargs), NS(bot=Bot(status)))
    assert bool(store.pending_topics()) == accepted


@pytest.mark.asyncio
async def test_quiet_messages_commands_and_duplicate_help(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    adapter = SalesTelegram(-100, SalesService(store, Fake()))
    context = NS(bot=Bot())
    await adapter.ingest(update(text='спасибо'), context)
    assert not store.pending_topics()
    help_update = update(text='/help')
    help_update.update_id = help_update.effective_message.message_id = 2
    await adapter.ingest(help_update, context)
    await adapter.ingest(help_update, context)
    assert len(store.deliveries()) == 1
    assert not context.bot.sent
    other_bot_command = update(text='/help@another_bot')
    other_bot_command.update_id = other_bot_command.effective_message.message_id = 3
    await adapter.ingest(other_bot_command, context)
    assert len(store.deliveries()) == 1


@pytest.mark.asyncio
async def test_background_delivery_resumes_without_model_and_analysis_not_blocking_ingest(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    started, release = asyncio.Event(), asyncio.Event()
    class Slow(Fake):
        async def analyze(self, context):
            started.set()
            await release.wait()
            return result(context)
    service = SalesService(store, Slow())
    adapter = SalesTelegram(-100, service)
    ctx = NS(bot=Bot())
    await adapter.ingest(update(), ctx)
    task = asyncio.create_task(service.process(-100, 10))
    await asyncio.wait_for(started.wait(), 2)
    another = update(topic=20, text='Вторая сделка')
    another.update_id = another.effective_message.message_id = 2
    await asyncio.wait_for(adapter.ingest(another, ctx), 2)
    assert (-100, 20) in store.pending_topics()
    release.set()
    await task
    reopened = SalesStore(store.path)
    adapter = SalesTelegram(-100, SalesService(reopened, Fake()))
    loop = asyncio.create_task(adapter.deliver_pending(NS(bot=ctx.bot)))
    for _ in range(100):
        if ctx.bot.sent: break
        await asyncio.sleep(.01)
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    assert ctx.bot.sent[0]['chat_id'] == -100
    assert ctx.bot.sent[0]['message_thread_id'] == 10
    assert not reopened.deliveries()


def test_settings_use_users_existing_names_and_isolated_paths(tmp_path, monkeypatch):
    for name in ('TELEGRAM_SALES_BADDY_BOT_TOKEN', 'OWNER_SALES_BADDY_CHAT_ID',
                 'SALES_BUDDY_RUNTIME_DIR', 'SALES_BUDDY_CODEX_HOME', 'TELEGRAM_BOT_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    file = tmp_path / '.env'
    file.write_text('TELEGRAM_BOT_TOKEN=rick-secret\nTELEGRAM_SALES_BADDY_BOT_TOKEN=sali-secret\n'
        'OWNER_SALES_BADDY_CHAT_ID=-100\nOWNER_SALES_BADDY_CODEX_MODEL=custom-model\n'
        'OWNER_SALES_BADDY_CODEX_REASONING_EFFORT=medium\n', encoding='utf-8')
    settings = SalesSettings.load(tmp_path)
    assert settings.model == 'custom-model' and settings.effort == 'medium'
    assert settings.chat_id == -100
    assert 'secret' not in repr(settings)
    assert settings.runtime == tmp_path / 'runtime' / 'sales_buddy'
    monkeypatch.setenv('TELEGRAM_SALES_BADDY_BOT_TOKEN', 'rick-secret')
    with pytest.raises(ValueError): SalesSettings.load(tmp_path)
    monkeypatch.setenv('TELEGRAM_SALES_BADDY_BOT_TOKEN', 'sali-secret')
    monkeypatch.setenv('SALES_BUDDY_RUNTIME_DIR', str(tmp_path / 'owner_context'))
    with pytest.raises(ValueError): SalesSettings.load(tmp_path)


@pytest.mark.asyncio
async def test_codex_tool_free_ephemeral_no_rick_context_or_credentials(tmp_path, monkeypatch):
    starts, configs, turns = [], [], []
    class Thread:
        id = 'sales-ephemeral'
        def run(self, prompt, **kwargs):
            turns.append((prompt, kwargs))
            return NS(error=None, items=[], final_response=json.dumps(dict(answer='Один вопрос?',
                facts=[], basis=[], goal='', next_action='')))
    class SDK:
        def __init__(self, config): configs.append(config)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def thread_start(self, **kwargs): starts.append(kwargs); return Thread()
    monkeypatch.setattr('agentbridge.sales.provider.Codex', SDK)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'RICK_PRIVATE')
    monkeypatch.setenv('OPENAI_API_KEY', 'PRIVATE_API')
    context_file = tmp_path / 'working_context.md'
    context_file.write_text('PRIVATE_RICK_CONTEXT', encoding='utf-8')
    def forbidden(*args, **kwargs):
        raise AssertionError('No commands allowed')
    monkeypatch.setattr('agentbridge.agents.codex.subprocess.run', forbidden)
    provider = SalesCodexProvider(model='gpt-6-luna', reasoning_effort='medium', cwd=tmp_path,
        codex_home=tmp_path / 'codex', knowledge='Один следующий шаг')
    assert (await provider.analyze(dict(events=[])))['answer'] == 'Один вопрос?'
    assert starts[0]['ephemeral'] is True
    assert starts[0]['sandbox'] == turns[0][1]['sandbox'] == Sandbox.read_only
    assert starts[0]['approval_mode'] == ApprovalMode.deny_all
    assert configs[0].env['TELEGRAM_BOT_TOKEN'] == configs[0].env['OPENAI_API_KEY'] == ''
    assert 'features.shell_tool=false' in configs[0].config_overrides
    assert 'mcp_servers={}' in configs[0].config_overrides
    assert 'owner_context' not in str(starts) + str(turns)
    assert 'PRIVATE_RICK_CONTEXT' not in str(starts) + str(turns)
    assert context_file.read_text(encoding='utf-8') == 'PRIVATE_RICK_CONTEXT'
    validate_structured_output_schema(SCHEMA)


def test_application_separate_token_handler_and_runtime(tmp_path):
    settings = SalesSettings('123456:fake-secret-for-test-only', -100, tmp_path, tmp_path / 'codex', 'model', 'medium')
    app = create_sales_application(settings, SalesService(SalesStore(tmp_path / 'sales.sqlite3'), Fake()))
    assert len(app.handlers[0]) == 1
    assert app.post_init and app.post_stop
    assert not (tmp_path / 'agentbridge.sqlite3').exists()


@pytest.mark.asyncio
async def test_cancelled_analysis_is_recoverable(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store)
    entered = asyncio.Event()
    class Waiting:
        async def analyze(self, context):
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(SalesService(store, Waiting()).process(-100, 10))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    reopened = SalesStore(store.path)
    assert reopened.pending_topics() == [(-100, 10)]
    assert reopened.deliveries() == []
    await SalesService(reopened, Fake()).process(-100, 10)
    assert len(reopened.deliveries()) == 1


def test_long_original_is_retained_with_explicit_excerpt_flag(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    text = 'А' * 20000
    ingest(store, text=text)
    event = store.context(-100, 10)['events'][0]
    assert len(event['text']) == 16000 and event['truncated'] is True
    assert event['original_length'] == 20000
    with store.connect() as db:
        assert db.execute('SELECT text FROM events').fetchone()[0] == text


@pytest.mark.asyncio
async def test_membership_failure_persists_and_recovers_without_leaking_to_model(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    fake = Fake()
    adapter = SalesTelegram(-100, SalesService(store, fake))
    class Unavailable(Bot):
        async def get_chat_member(self, *args):
            raise OSError('temporary Telegram error')
    await adapter.ingest(update(), NS(bot=Unavailable()))
    assert len(store.awaiting_authorization(-100)) == 1
    assert not store.pending_topics()
    await adapter.service.process(-100, 10)
    assert not fake.contexts
    reopened = SalesStore(store.path)
    adapter = SalesTelegram(-100, SalesService(reopened, fake))
    await adapter.authorize_pending(Bot())
    await adapter.service.process(-100, 10)
    assert len(fake.contexts) == 1
    assert not reopened.awaiting_authorization(-100)
    denied = update(text='Недопустимый контекст')
    denied.update_id = denied.effective_message.message_id = 2
    await adapter.ingest(denied, NS(bot=Unavailable()))
    await adapter.authorize_pending(Bot('left'))
    assert not reopened.awaiting_authorization(-100)
    assert not reopened.context(-100, 10)['events']
    assert all(row['text'] != 'Недопустимый контекст' for row in reopened.context(-100, 10)['history'])


@pytest.mark.asyncio
async def test_attachment_caption_never_implies_read_contents(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    adapter = SalesTelegram(-100, SalesService(store, Fake()))
    await adapter.ingest(update(text='', caption='Вот КП, посмотри', document=NS(file_id='fake')), NS(bot=Bot()))
    row = store.context(-100, 10)['events'][0]
    assert row['source'] == 'unsupported_attachment'
    assert 'не разобрано' in row['text'] and 'Вот КП' in row['text']


def test_large_card_is_bounded_without_erasing_old_confirmed_facts(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, text='Что известно про редкийартикул?')
    with store.connect() as db:
        for i in range(150):
            text = 'редкийартикул согласован' if i == 0 else f'Подтверждённое условие {i}'
            db.execute("INSERT INTO facts(chat_id,topic_id,field,text,kind,event_id,quote) VALUES (-100,10,'confirmed',?,'fact',1,?)", (text,text))
    context = store.context(-100, 10)
    assert any(f['text'] == 'редкийартикул согласован' for f in context['facts'])
    assert len(context['facts']) <= 16
    assert context['omitted_facts'] >= 134
    # Further updates still work; a large ledger must not permanently stop analysis.
    patch = validate_result(result(context), context)
    assert store.complete(-100, 10, context['events'], patch)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM facts').fetchone()[0] == 151


def test_recommendation_can_cite_visible_old_fact_source(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store)
    context = store.context(-100, 10)
    context['facts'] = [dict(id=9, event_id=77, field='client', text='Птицефабрика',
                            kind='fact', quote='Птицефабрика')]
    payload = result(context)
    payload['basis'] = [77]
    assert validate_result(payload, context)['basis'] == [77]
    payload['basis'] = [88]
    with pytest.raises(ValueError):
        validate_result(payload, context)
