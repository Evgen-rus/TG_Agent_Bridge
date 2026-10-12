import asyncio
import json
import threading
from types import SimpleNamespace as NS

import pytest

from agentbridge.sales.editing import apply_edits, EDITOR_INSTRUCTIONS, MANAGER_STYLE
from agentbridge.sales.provider import SalesCodexProvider
from agentbridge.sales.service import SalesService
from agentbridge.sales.storage import SalesStore
from test_sales_buddy import ingest


def result(text='На данный момент осуществим проверку линии Ивана к 15 октября за 12000 рублей.'):
    return dict(answer=text, goal='Согласовать обследование', next_action='Подтвердить время',
                facts=[], basis=[1], client_draft=True, communication_profiles=[])


def edit(**extra):
    return dict(defects=['Канцелярское вступление и отглагольная конструкция'],
                edits=[dict(old='На данный момент', new='Сейчас'),
                       dict(old='осуществим проверку линии', new='проверим линию')],
                facts_preserved=True, commitments_preserved=True,
                goal=result()['goal'], next_action=result()['next_action'], **extra)


def test_style_pass_preserves_names_numbers_promises_goal_and_step():
    original = result()
    changed = apply_edits(original, edit())
    assert changed['answer'] == 'Сейчас проверим линию Ивана к 15 октября за 12000 рублей.'
    assert {k: v for k, v in changed.items() if k != 'answer'} == {
        k: v for k, v in original.items() if k != 'answer'}
    assert original['answer'].startswith('На данный момент')


@pytest.mark.parametrize('old,new', [
    ('12000', '10000'), ('к 15 октября', 'к 14 октября'), ('Ивана', 'Петра'),
    ('осуществим проверку', 'гарантируем результат'), ('за 12000 рублей', 'бесплатно'),
    ('возможно', 'точно'), ('нельзя', 'можно'), ('Направляю вам', 'Обещаю вам'),
])
def test_unapproved_edits_fall_back_whole_pass(old, new):
    original = result(original_text := result()['answer'] + ' Возможно, нельзя. Направляю вам документ.')
    candidate = edit()
    candidate['edits'].append(dict(old=old, new=new))
    assert apply_edits(original, candidate)['answer'] == original_text


@pytest.mark.parametrize('field,value', [('facts_preserved', False), ('commitments_preserved', False),
    ('goal', 'Дать скидку'), ('next_action', 'Подписать договор'), ('defects', []), ('edits', 'invalid')])
def test_editor_must_preserve_contract(field, value):
    candidate = edit()
    candidate[field] = value
    assert apply_edits(result(), candidate) == result()


def test_quotes_and_identifier_fragments_are_immutable():
    for text in ('Клиент: "На данный момент осуществим проверку".',
                 'Клиент: «На данный момент осуществим проверку».',
                 'XНа данный моментY осуществим проверку'):
        assert apply_edits(result(text), edit()) == result(text)


@pytest.mark.parametrize('draft,old,new,expected', [
    ('Направляю вам документ. Проверьте совместимость до пятницы.',
     'Направляю вам', 'Отправляю вам', 'Отправляю вам документ. Проверьте совместимость до пятницы.'),
    ('Можно произвести оплату по счёту на 15000 рублей до 12 ноября.',
     'произвести оплату по счёту', 'оплатить счёт', 'Можно оплатить счёт на 15000 рублей до 12 ноября.'),
    ('В настоящее время условия выезда не подтверждены. Уточним их до встречи.',
     'В настоящее время', 'Сейчас', 'Сейчас условия выезда не подтверждены. Уточним их до встречи.'),
])
def test_anonymized_sales_examples(draft, old, new, expected):
    candidate = edit()
    candidate['edits'] = [dict(old=old, new=new)]
    assert apply_edits(result(draft), candidate)['answer'] == expected


class SDK:
    starts = []
    runs = []
    draft = False
    fail_edit = False

    def __init__(self, config): self.config = config
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def thread_start(self, **kwargs):
        self.starts.append(kwargs)
        return NS(id=f'thread-{len(self.starts)}', run=self.run)
    def thread_resume(self, thread_id, **kwargs):
        return NS(id=thread_id, run=self.run)
    def run(self, prompt, **kwargs):
        data = json.loads(prompt)
        self.runs.append((data, kwargs))
        if 'draft' in data:
            if self.fail_edit:
                raise OSError('private prompt secret network')
            output = edit()
        else:
            output = result()
            output['client_draft'] = self.draft
        return NS(error=None, items=[], final_response=json.dumps(output))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    SDK.starts, SDK.runs, SDK.draft, SDK.fail_edit = [], [], False, False
    monkeypatch.setattr('agentbridge.sales.provider.Codex', SDK)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    provider = SalesCodexProvider(model='fake', reasoning_effort='medium', cwd=tmp_path,
        codex_home=tmp_path / 'codex', knowledge='rules', store=store)
    ingest(store, text='Сали, подготовь письмо. Линия Ивана, 15 октября, 12000 рублей.')
    return store, provider


@pytest.mark.asyncio
async def test_ordinary_answer_single_turn_and_draft_isolated_low_editor(setup):
    store, provider = setup
    ordinary = await provider.analyze(store.context(-100, 10))
    assert len(SDK.runs) == 1 and ordinary['answer'].startswith('На данный момент')
    SDK.draft = True
    ingest(store, 2, text='Напиши готовый текст клиенту')
    context = store.context(-100, 10)
    drafted = await provider.analyze(context)
    assert drafted['answer'].startswith('Сейчас проверим')
    editor = SDK.starts[-1]
    assert editor['ephemeral'] and editor['approval_mode'].value == 'deny_all'
    assert editor['sandbox'].value == 'read-only'
    assert editor['developer_instructions'] == EDITOR_INSTRUCTIONS
    assert editor['config']['model_reasoning_effort'] == 'low'
    assert SDK.runs[-1][1]['effort'] == 'low'
    assert 'facts' not in SDK.runs[-1][0]  # No new sales analysis or raw history.
    before = len(SDK.runs)
    assert await provider.analyze(context) == drafted
    assert len(SDK.runs) == before
    assert store.thread(-100, 10)['thread_id'] != f'thread-{len(SDK.starts)}'


@pytest.mark.asyncio
async def test_edit_error_keeps_draft_outbox_and_backoff(setup, caplog):
    store, provider = setup
    SDK.draft = SDK.fail_edit = True
    await SalesService(store, provider, system_chat_id=-100).process(-100, 10)
    assert not store.context(-100, 10)['events']
    assert any(row['text'] == result()['answer'] for row in store.deliveries())
    assert store.op_get('codex_failure')
    assert 'private prompt secret' not in caplog.text


def test_trusted_rules_no_per_answer_editor_and_preference_precedence():
    from agentbridge.sales.provider import INSTRUCTIONS, TOOL_FREE
    assert MANAGER_STYLE in INSTRUCTIONS and 'Прямые пожелания клиента приоритетнее' in INSTRUCTIONS
    assert 'features.shell_tool=false' in TOOL_FREE
    assert 'Сначала перечисли все реальные дефекты' in EDITOR_INSTRUCTIONS


@pytest.mark.asyncio
async def test_cancel_stops_typing_before_uninterruptible_sdk_finishes(setup, monkeypatch):
    from agentbridge.sales.activity import SalesActivity
    store, provider = setup
    entered, release = threading.Event(), threading.Event()
    def slow(context):
        entered.set()
        release.wait(3)
        return result()
    monkeypatch.setattr(provider, '_analyze', slow)
    activity = SalesActivity()
    bot = NS(send_chat_action=lambda **kwargs: asyncio.sleep(0))
    service = SalesService(store, provider)
    task = asyncio.create_task(service.process(-100, 10,
        activity=lambda chat, topic: activity.scope(bot, chat, topic)))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(.03)
    assert not activity.tasks and service._lock.locked()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert not service._lock.locked() and store.context(-100, 10)['events']


@pytest.mark.asyncio
async def test_unsupported_profile_is_not_delivered_as_certain_claim(setup, monkeypatch):
    from test_sales_communication import profile
    store, provider = setup
    calls = []
    def run(thread, ctx, schema=None, **kwargs):
        calls.append(ctx)
        if 'profile_guard' in ctx:
            return {**result('Пока недостаточно клиентских реплик. Использую нейтральный тон.'),
                    'client_draft': False}
        return {**result('Клиент точно C.'), 'client_draft': False,
                'communication_profiles': [profile()]}
    monkeypatch.setattr(provider, '_run', run)
    await SalesService(store, provider).process(-100, 10)
    assert len(calls) == 2
    assert 'profile_guard' in calls[-1]
    assert all('точно C' not in d['text'] for d in store.deliveries())
    assert store.context(-100, 10)['communication_profiles'][0]['status'] == 'insufficient_evidence'


@pytest.mark.asyncio
async def test_long_transcript_observations_combined_only_after_all_parts(setup, monkeypatch):
    from test_sales_communication import profile, LINES
    store, provider = setup
    # Finish the initial unrelated event, then one source spanning two parts.
    with store.connect() as db:
        db.execute('UPDATE events SET processed=1')
    text = LINES[0] + '\n' + 'менеджер: фон\n' * 1400 + LINES[1]
    ingest(store, 2, text=text)
    calls = []
    def run(thread, ctx, schema=None, **kwargs):
        calls.append(ctx)
        update = profile(event_id=2)
        if 'document_part' in ctx:
            update['status'] = 'tentative'
            update['evidence'] = [e for e in update['evidence'] if e['role_quote'] in ctx['events'][0]['text']]
            return {**result('Наблюдение части'), 'facts': [], 'basis': [2],
                    'client_draft': False, 'communication_profiles': [update] if update['evidence'] else []}
        assert ctx['events'][0]['fully_read']
        observations = [p for part in ctx['events'][0]['document_analysis']
                        for p in part['communication_observations']]
        assert len(observations) == 2
        return {**result('Предварительно C с D, по двум клиентским наблюдениям.'),
                'basis': [2], 'client_draft': False, 'communication_profiles': [update]}
    monkeypatch.setattr(provider, '_run', run)
    await SalesService(store, provider).process(-100, 10)
    saved = store.context(-100, 10)['communication_profiles'][0]
    assert saved['status'] == 'supported' and len(saved['evidence']) == 2
    assert len(store.profile_history(-100, 10, 'Иван')) == 1
    assert sum('document_part' not in ctx for ctx in calls) == 1
