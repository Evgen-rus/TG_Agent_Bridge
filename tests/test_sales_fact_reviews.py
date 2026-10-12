import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from agentbridge.sales.bot import SalesTelegram
from agentbridge.sales.service import SalesService, validate_result
from agentbridge.sales.storage import SalesStore


def test_profile_repair_cannot_silently_remove_fact_confirmation(tmp_path, monkeypatch):
    from agentbridge.sales.provider import SalesCodexProvider
    from test_sales_threads import SDK
    from test_sales_communication import profile
    store, context, result, old = seeded(tmp_path)
    monkeypatch.setattr('agentbridge.sales.provider.Codex', SDK)
    provider = SalesCodexProvider(model='fake', reasoning_effort='medium', cwd=tmp_path,
        codex_home=tmp_path/'codex', knowledge='rules', store=store)
    def run(thread, current, schema=None, **kwargs):
        if 'profile_guard' in current:
            return {**result, 'facts': [], 'communication_profiles': []}
        return {**result, 'communication_profiles': [profile(event_id=2)]}
    monkeypatch.setattr(provider, '_run', run)
    validated = provider._analyze(context)
    assert validated['facts'] == [] and len(validated['fact_reviews']) == 1
    store.complete(-100, 10, context['events'], validated)
    assert store.awaiting_fact_review(-100, 10)
    assert active_facts(store)[0]['text'] == old['text']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,probe_calls', [('codex_turn_failed', 1), ('codex_usage_limit', 0), ('codex_network', 0)])
async def test_restart_checks_old_validation_flag_without_bypassing_real_outage_backoff(tmp_path, kind, probe_calls):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    store.op_set('codex_failure', dict(kind=kind, episode='test', attempts=1, retry_at=time.time()+300))
    provider = NS(probe=AsyncMock(return_value=None))
    service = SalesService(store, provider)
    adapter = SalesTelegram(-100, service)
    adapter.run_id = store.begin_run(-100, 0)
    await adapter.startup_probe(NS(running=True))
    assert provider.probe.await_count == probe_calls
    assert bool(store.op_get('codex_failure')) is (probe_calls == 0)


def ingest(store, update, text, topic=10):
    return store.ingest(update_id=update, chat_id=-100, topic_id=topic, title='deal',
        message_id=update, author_id=7, author='Manager', date='2026-10-12', text=text)


def patch(event, text, replaces=0, field='confirmed', kind='fact'):
    return dict(field=field, text=text, kind=kind, event_id=event['id'], quote=event['text'], replaces=replaces)


def answer(facts, event, text='Скрытая рекомендация до решения менеджера'):
    return dict(answer=text, facts=facts, goal='Уточнить условия', next_action='Задать вопрос', basis=[event['id']])


def seeded(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, 1, 'Обследование платное')
    context = store.context(-100, 10)
    event = context['events'][0]
    assert store.complete(-100, 10, context['events'], answer([patch(event, event['text'])], event, 'Первый ответ'))
    for row in store.deliveries():
        store.delivered(row['id'], 90)
    ingest(store, 2, 'Обследование включено в стоимость оборудования')
    context = store.context(-100, 10)
    event, old = context['events'][0], context['facts'][0]
    proposal = patch(event, event['text'], replaces=old['id'])
    result = answer([proposal], event)
    return store, context, result, old


def pending_review(tmp_path):
    store, context, result, old = seeded(tmp_path)
    validated = validate_result(result, context, allow_fact_reviews=True)
    assert validated['facts'] == [] and len(validated['fact_reviews']) == 1
    assert store.complete(-100, 10, context['events'], validated)
    rows = store.deliveries()
    assert len(rows) == 1 and rows[0]['delivery_key'].startswith('fact-review:')
    review_id = int(rows[0]['delivery_key'].split(':')[1])
    store.delivered(rows[0]['id'], 100)
    return store, context, result, old, review_id


def active_facts(store):
    with store.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM facts WHERE superseded_by IS NULL')]


def test_validator_disputed_patch_review_and_default_strict(tmp_path):
    _, context, result, _ = seeded(tmp_path)
    with pytest.raises(ValueError, match='Unsupported fact replacement'):
        validate_result(result, context)
    reviewed = validate_result(result, context, allow_fact_reviews=True)
    assert reviewed['facts'] == []
    assert reviewed['fact_reviews'] == result['facts']
    result['facts'][0]['quote'] = 'Нет такого источника'
    with pytest.raises(ValueError):
        validate_result(result, context, allow_fact_reviews=True)


def test_identical_replacement_is_skipped_even_without_correction(tmp_path):
    _, context, _, old = seeded(tmp_path)
    same = patch(context['events'][0], old['text'], replaces=old['id'])
    result = validate_result(answer([same], context['events'][0]), context, allow_fact_reviews=True)
    assert result['facts'] == [] and not result.get('fact_reviews')
    assert not validate_result(answer([same], context['events'][0]), context)['facts']


@pytest.mark.parametrize('invalid', ['field', 'replacement', 'duplicate', 'source'])
def test_review_does_not_bypass_trust_validation(tmp_path, invalid):
    _, context, result, _ = seeded(tmp_path)
    if invalid == 'field':
        result['facts'][0]['field'] = 'client'
    elif invalid == 'replacement':
        result['facts'][0]['replaces'] = 99999
    elif invalid == 'duplicate':
        result['facts'].append(dict(result['facts'][0]))
    else:
        result['facts'][0]['event_id'] = 99999
    with pytest.raises(ValueError):
        validate_result(result, context, allow_fact_reviews=True)


def test_pending_review_holds_whole_result_and_survives_restart(tmp_path):
    store, context, result, old, review = pending_review(tmp_path)
    assert store.awaiting_fact_review(-100, 10)
    assert not store.awaiting_fact_review(-100, 20)
    assert active_facts(store)[0]['text'] == old['text']
    assert not store.deliveries()
    assert (-100, 10) not in store.pending_topics()
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM recommendations').fetchone()[0] == 1
    restarted = SalesStore(store.path)
    assert restarted.awaiting_fact_review(-100, 10)
    assert restarted.decide_fact_review(review, -100, 10, 100, 7, True) == 'accepted'
    assert active_facts(restarted)[0]['text'] == context['events'][0]['text']
    assert not restarted.awaiting_fact_review(-100, 10)
    assert [row['text'] for row in restarted.deliveries()] == [result['answer']]
    assert restarted.decide_fact_review(review, -100, 10, 100, 7, True) == 'already'
    with restarted.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM recommendations').fetchone()[0] == 2
        decision = db.execute('SELECT decided_by,decided_at FROM fact_reviews WHERE id=?', (review,)).fetchone()
        assert decision['decided_by'] == 7 and decision['decided_at'] > 0


def test_rejection_preserves_old_fact_and_withholds_unsafe_answer_once(tmp_path):
    store, context, result, old, review = pending_review(tmp_path)
    assert store.decide_fact_review(review, -100, 10, 100, 7, False) == 'rejected'
    assert active_facts(store)[0]['text'] == old['text']
    assert not store.awaiting_fact_review(-100, 10)
    assert not store.context(-100, 10)['events']
    delivery = store.deliveries()
    assert len(delivery) == 1 and result['answer'] not in delivery[0]['text']
    assert store.decide_fact_review(review, -100, 10, 100, 8, True) == 'already'
    assert store.deliveries() == delivery
    with store.connect() as db:
        records = [json.loads(row[0]) for row in db.execute('SELECT payload FROM recommendations ORDER BY id')]
        assert len(records) == 2 and records[-1]['answer'] != result['answer']
        assert records[-1]['facts'] == [] and records[-1]['next_action'] == ''


@pytest.mark.parametrize('chat,topic,message', [(-200, 10, 100), (-100, 20, 100), (-100, 10, 101)])
def test_storage_rejects_forged_review_scope(tmp_path, chat, topic, message):
    store, _, _, old, review = pending_review(tmp_path)
    assert store.decide_fact_review(review, chat, topic, message, 7, True) == 'denied'
    assert store.awaiting_fact_review(-100, 10)
    assert active_facts(store)[0]['text'] == old['text']


def test_stale_old_fact_cannot_be_superseded_again(tmp_path):
    store, _, result, old, review = pending_review(tmp_path)
    with store.connect() as db:
        db.execute('UPDATE facts SET superseded_by=999 WHERE id=?', (old['id'],))
    assert store.decide_fact_review(review, -100, 10, 100, 7, True) == 'stale'
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 1
        records = [json.loads(row[0]) for row in db.execute('SELECT payload FROM recommendations ORDER BY id')]
        assert len(records) == 2 and records[-1]['answer'] != result['answer']
        assert records[-1]['facts'] == [] and records[-1]['next_action'] == ''
    assert all(result['answer'] not in row['text'] for row in store.deliveries())


@pytest.mark.parametrize('accept_final', [False, True])
def test_all_reviews_required_and_any_rejection_discards_entire_episode(tmp_path, accept_final):
    store, context, result, old = seeded(tmp_path)
    with store.connect() as db:
        second = db.execute('''INSERT INTO facts(chat_id,topic_id,field,text,kind,event_id,quote)
            VALUES(-100,10,'stage','Ожидание оплаты','fact',1,'Обследование платное')''').lastrowid
    context = store.context(-100, 10)
    event = context['events'][0]
    result['facts'].append(patch(event, 'Условия согласуются', replaces=second, field='stage'))
    assert store.complete(-100, 10, context['events'], validate_result(result, context, allow_fact_reviews=True))
    reviews = []
    for index, row in enumerate(store.deliveries()):
        reviews.append(int(row['delivery_key'].split(':')[1]))
        store.delivered(row['id'], 100 + index)
    assert len(reviews) == 2
    assert store.decide_fact_review(reviews[0], -100, 10, 100, 7, True) == 'accepted'
    assert store.awaiting_fact_review(-100, 10)
    assert len(active_facts(store)) == 2 and active_facts(store)[0]['text'] == old['text']
    assert not store.deliveries()
    assert store.decide_fact_review(reviews[1], -100, 10, 101, 7, accept_final) == ('accepted' if accept_final else 'rejected')
    assert len(active_facts(store)) == 2
    with store.connect() as db:
        records = [json.loads(row[0]) for row in db.execute('SELECT payload FROM recommendations ORDER BY id')]
        assert len(records) == 2
        if not accept_final:
            assert records[-1]['answer'] != result['answer'] and records[-1]['facts'] == []
    if accept_final:
        assert {fact['text'] for fact in active_facts(store)} == {event['text'], 'Условия согласуются'}


def test_pending_episode_duplicate_complete_no_duplicate_cards(tmp_path):
    store, context, result, _, _ = pending_review(tmp_path)
    assert not store.complete(-100, 10, context['events'], validate_result(result, context, allow_fact_reviews=True))
    # A concurrently finished result cannot bypass the already pending review.
    assert not store.complete(-100, 10, context['events'], {**result, 'facts': []})
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM fact_reviews').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM review_episodes').fetchone()[0] == 1


def test_safe_append_waits_with_disputed_patch_until_approval(tmp_path):
    store, context, result, old = seeded(tmp_path)
    event = context['events'][0]
    result['facts'].append(patch(event, 'Согласовать стоимость', field='next_step', kind='manager_action'))
    store.complete(-100, 10, context['events'], validate_result(result, context, allow_fact_reviews=True))
    assert len(active_facts(store)) == 1
    assert active_facts(store)[0]['text'] == old['text']
    row = store.deliveries()[0]
    store.delivered(row['id'], 100)
    review = int(row['delivery_key'].split(':')[1])
    assert store.decide_fact_review(review, -100, 10, 100, 7, True) == 'accepted'
    facts = active_facts(store)
    assert len(facts) == 2
    assert {f['text'] for f in facts} == {event['text'], 'Согласовать стоимость'}


def test_undelivered_review_cannot_be_approved(tmp_path):
    store, context, result, old = seeded(tmp_path)
    store.complete(-100, 10, context['events'], validate_result(result, context, allow_fact_reviews=True))
    row = store.deliveries()[0]
    review = int(row['delivery_key'].split(':')[1])
    assert store.decide_fact_review(review, -100, 10, 100, 7, True) == 'denied'
    assert active_facts(store)[0]['text'] == old['text']


@pytest.mark.asyncio
async def test_review_delivery_buttons_in_source_topic(tmp_path):
    store, context, result, old = seeded(tmp_path)
    store.complete(-100, 10, context['events'], validate_result(result, context, allow_fact_reviews=True))
    adapter = SalesTelegram(-100, SalesService(store, NS()))
    bot = NS(send_message=AsyncMock(return_value=NS(message_id=100)))
    await adapter.deliver_once(bot)
    options = bot.send_message.call_args.kwargs
    assert options['chat_id'] == -100 and options['message_thread_id'] == 10
    buttons = [button for row in options['reply_markup'].inline_keyboard for button in row]
    assert len(buttons) == 2
    assert any(button.callback_data.startswith('sales_fact:accept:') for button in buttons)
    assert any(button.callback_data.startswith('sales_fact:reject:') for button in buttons)
    assert result['answer'] not in options['text']
    assert old['text'] in options['text'] and context['events'][0]['text'] in options['text']


@pytest.mark.asyncio
async def test_service_disputed_fact_does_not_pause_codex_or_repeat_provider(tmp_path, caplog):
    store, context, result, old = seeded(tmp_path)
    provider = NS(analyze=AsyncMock(return_value=result))
    service = SalesService(store, provider)
    await service.process(-100, 10)
    assert store.awaiting_fact_review(-100, 10)
    assert not store.op_get('codex_failure')
    assert active_facts(store)[0]['text'] == old['text']
    await service.process(-100, 10)
    assert provider.analyze.await_count == 1
    assert result['answer'] not in caplog.text
    assert 'Unsupported fact replacement' not in caplog.text


@pytest.mark.asyncio
async def test_invalid_source_stops_only_episode_without_global_outage(tmp_path, caplog):
    store, context, result, old = seeded(tmp_path)
    result['facts'][0]['quote'] = 'SECRET nonexistent source'
    provider = NS(analyze=AsyncMock(return_value=result))
    service = SalesService(store, provider)
    await service.process(-100, 10)
    await service.process(-100, 10)
    assert provider.analyze.await_count == 1
    assert not store.awaiting_fact_review(-100, 10)
    assert not store.op_get('codex_failure')
    assert active_facts(store)[0]['text'] == old['text']
    with store.connect() as db:
        assert db.execute('SELECT processed FROM events WHERE update_id=2').fetchone()[0] == 3
    assert 'SECRET' not in caplog.text
    assert all(result['answer'] not in row['text'] for row in store.deliveries())


def click(review, chat=-100, topic=10, message=100, user=7):
    query = NS(data=f'sales_fact:accept:{review}', answer=AsyncMock(),
        message=NS(message_thread_id=topic, message_id=message, sender_chat=None))
    return NS(callback_query=query, effective_user=NS(id=user, is_bot=False),
              effective_chat=NS(id=chat))


@pytest.mark.asyncio
@pytest.mark.parametrize('status,allowed', [('member', True), ('administrator', True), ('creator', True), ('left', False), ('kicked', False)])
async def test_callback_current_group_members_only(tmp_path, status, allowed):
    store, _, _, old, review = pending_review(tmp_path)
    adapter = SalesTelegram(-100, SalesService(store, NS()))
    bot = NS(get_chat_member=AsyncMock(return_value=NS(status=status)))
    await adapter.callback(click(review), NS(bot=bot))
    assert store.awaiting_fact_review(-100, 10) is not allowed
    if not allowed:
        assert active_facts(store)[0]['text'] == old['text']


@pytest.mark.asyncio
@pytest.mark.parametrize('attack', ['foreign_chat', 'foreign_topic', 'forged_message', 'bot', 'anonymous', 'missing_user', 'membership_failure'])
async def test_callback_fail_closed(tmp_path, attack):
    store, _, _, old, review = pending_review(tmp_path)
    adapter = SalesTelegram(-100, SalesService(store, NS()))
    update = click(review)
    bot = NS(get_chat_member=AsyncMock(return_value=NS(status='member')))
    if attack == 'foreign_chat':
        update.effective_chat.id = -200
    elif attack == 'foreign_topic':
        update.callback_query.message.message_thread_id = 20
    elif attack == 'forged_message':
        update.callback_query.message.message_id = 999
    elif attack == 'bot':
        update.effective_user.is_bot = True
    elif attack == 'anonymous':
        update.callback_query.message.sender_chat = NS(id=-100)
    elif attack == 'missing_user':
        update.effective_user = None
    else:
        bot.get_chat_member.side_effect = RuntimeError('secret')
    await adapter.callback(update, NS(bot=bot))
    assert store.awaiting_fact_review(-100, 10)
    assert active_facts(store)[0]['text'] == old['text']
