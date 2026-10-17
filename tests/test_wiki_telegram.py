from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from agentbridge.learning.telegram import (
    candidate_keyboard, candidate_message, deliver_notifications, refinement_reply, review_callback,
)
from agentbridge.sales.bot import SalesTelegram


def candidate():
    return dict(id=1, version=2, status='pending', title='Обещания',
        situation='Когда обсуждают сроки', behavior='Уточни дату', rationale='Исправление команды',
        sources=[dict(text='Назови точную дату', url='https://t.me/c/123/17')])


def update(action='accept'):
    query = NS(data=f'wiki:{action}:1:2', message=NS(message_id=9, message_thread_id=0),
        answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    return NS(callback_query=query, effective_user=NS(id=7, is_bot=False),
        effective_chat=NS(id=-100), effective_message=NS(text='Сначала спроси дату',
            reply_to_message=NS(message_id=21)))


def test_render_candidate_and_unabridged_rule():
    row = candidate()
    assert 'Когда применять: Когда обсуждают сроки' in candidate_message(row)
    assert 'https://t.me/c/123/17' in candidate_message(row)
    assert [b.callback_data for b in candidate_keyboard(row).inline_keyboard[0]] == [
        'wiki:accept:1:2', 'wiki:refine:1:2', 'wiki:reject:1:2']
    row.update(title='я'*5000, situation='я'*5000, behavior='я'*5000, rationale='я'*5000)
    row['sources'] *= 20
    assert row['behavior'] in candidate_message(row)
    assert row['situation'] in candidate_message(row)


@pytest.mark.asyncio
async def test_delivery_failure_stays_pending_and_success_acknowledges():
    store = NS(pending_notifications=Mock(return_value=[dict(id=3, payload=candidate())]),
        notification_delivered=Mock())
    bot = NS(send_message=AsyncMock(side_effect=RuntimeError('network')))
    await deliver_notifications(store, bot, -100)
    store.notification_delivered.assert_not_called()
    bot.send_message.side_effect = None
    bot.send_message.return_value = NS(message_id=77)
    await deliver_notifications(store, bot, -100)
    store.notification_delivered.assert_called_once_with(3, 77)
    assert bot.send_message.call_args.kwargs['message_thread_id'] is None


@pytest.mark.asyncio
async def test_long_rule_is_split_losslessly_before_acceptance_buttons():
    row = candidate()
    row.update(title='Заголовок '*150, situation='Когда '*250,
        behavior='Что делать '*130, rationale='Почему '*180)
    store = NS(pending_notifications=Mock(return_value=[dict(id=3, payload=row)]),
        notification_delivered=Mock())
    bot = NS(send_message=AsyncMock(return_value=NS(message_id=77)))
    await deliver_notifications(store, bot, -100)
    calls = bot.send_message.call_args_list
    assert len(calls) > 1
    assert all(len(c.kwargs['text'].encode('utf-16-le')) // 2 <= 4096 for c in calls)
    assert all(c.kwargs['reply_markup'] is None for c in calls[:-1])
    assert calls[-1].kwargs['reply_markup'] is not None
    restored = ''.join(c.kwargs['text'].split('\n\n', 1)[1] for c in calls)
    assert restored == candidate_message(row)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['accept', 'reject', 'disable'])
async def test_callback_uses_shared_decision_and_version(action):
    store = NS(get=Mock(return_value=candidate()), decide=Mock())
    item = update(action)
    await review_callback(store, item, NS(), True)
    store.decide.assert_called_once_with(1, action, '7', expected_version=2)
    item.callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_stale_and_unauthorized_callbacks_do_not_decide():
    store = NS(get=Mock(return_value={**candidate(), 'version': 3}), decide=Mock())
    await review_callback(store, update(), NS(), True)
    await review_callback(store, update(), NS(), False)
    store.decide.assert_not_called()


@pytest.mark.asyncio
async def test_refine_prompt_and_reply_requires_separate_acceptance():
    store = NS(get=Mock(return_value=candidate()), decide=Mock(), save_prompt=Mock(),
        prompt=Mock(return_value=dict(candidate_id=1, version=2)))
    context = NS(bot=NS(send_message=AsyncMock(return_value=NS(message_id=21))))
    item = update('refine')
    await review_callback(store, item, context, True)
    store.save_prompt.assert_called_once_with(-100, 21, 1, 2)
    store.decide.assert_not_called()
    assert await refinement_reply(store, item, context, True)
    store.decide.assert_called_once_with(1, 'refine', '7', comment='Сначала спроси дату',
        expected_version=2, changes={'behavior': 'Сначала спроси дату'})


@pytest.mark.asyncio
async def test_sally_group_member_can_review_and_removed_member_cannot():
    store = NS(get=Mock(return_value=candidate()), decide=Mock())
    adapter = SalesTelegram(-100, NS(learning=store))
    bot = NS(get_chat_member=AsyncMock(return_value=NS(status='member')))
    await adapter.callback(update(), NS(bot=bot))
    assert store.decide.call_count == 1
    bot.get_chat_member.return_value = NS(status='left')
    await adapter.callback(update(), NS(bot=bot))
    assert store.decide.call_count == 1
    bot.get_chat_member.side_effect = RuntimeError('network')
    await adapter.callback(update(), NS(bot=bot))
    assert store.decide.call_count == 1


@pytest.mark.asyncio
async def test_rick_review_only_from_configured_owner_identity_and_chat():
    from agentbridge.telegram.bot import create_telegram_application
    store = NS(get=Mock(return_value=candidate()), decide=Mock())
    application = create_telegram_application(token='test-token', owner_chat_id=7,
        message_service=NS(learning=store), batch_seconds=0)
    callback = next(handler.callback for handler in application.handlers[0]
        if handler.callback.__name__ == 'wiki_callback')
    item = update()
    item.effective_chat.id = 7
    await callback(item, NS())
    assert store.decide.call_count == 1
    item.effective_user.id = 8
    await callback(item, NS())
    item.effective_user.id, item.effective_chat.id = 7, -100
    await callback(item, NS())
    assert store.decide.call_count == 1


@pytest.mark.asyncio
async def test_queue_and_refinement_survive_store_reopen(tmp_path):
    from agentbridge.learning.store import LearningStore
    path = tmp_path / 'sales.sqlite3'
    store = LearningStore(path)
    source = dict(source_id='event:1', version='v1', text='Уточни дату', url='https://t.me/c/123/17',
        context='0', author='7', date='2026-10-17')
    lesson = {k: v for k, v in candidate().items() if k not in {'id', 'version', 'status', 'sources'}}
    lesson.update(scope='global', context='', source_ids=['event:1'])
    ident = store.commit_batch([source], [lesson])[0]
    bot = NS(send_message=AsyncMock(side_effect=RuntimeError('network')))
    await deliver_notifications(store, bot, -100)
    store = LearningStore(path)
    assert len(store.pending_notifications()) == 1
    bot.send_message.side_effect = None
    bot.send_message.return_value = NS(message_id=77)
    await deliver_notifications(store, bot, -100)
    assert not LearningStore(path).pending_notifications()
    store.save_prompt(-100, 21, ident, 1)
    store = LearningStore(path)
    item = update('refine')
    assert await refinement_reply(store, item, NS(bot=bot), True)
    snapshot = LearningStore(path).snapshot()
    assert snapshot['candidates'][0]['behavior'] == item.effective_message.text
    assert snapshot['candidates'][0]['version'] == 2
    assert snapshot['rules'] == []
