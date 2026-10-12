from types import SimpleNamespace as NS

import pytest

from agentbridge.sales.bot import SalesTelegram
from agentbridge.sales.service import SalesService
from agentbridge.sales.storage import SalesStore, should_respond
from test_sales_buddy import Bot, Fake, update


@pytest.mark.parametrize('name', ['Сали', 'Салли', 'Sali', 'Salli', 'Saly', 'Sally', 'SALLY', '@sali_bot'])
def test_name_overrides_reply_to_another_member(name):
    assert should_respond(f'{name}, подготовь ответ', 'sali_bot', True)


@pytest.mark.parametrize('text', ['спасибо', ' ок ', '@another_bot подготовь ответ', 'Салима ответит', 'Sallyann ответит'])
def test_unaddressed_member_conversation_does_not_trigger(text):
    assert not should_respond(text, 'sali_bot', True)


@pytest.mark.asyncio
@pytest.mark.parametrize('reply_id,author_id,expected', [(10, 8, True), (55, Bot.id, True), (55, 8, False)])
async def test_topic_anchor_and_bot_reply_trigger_but_member_reply_does_not(tmp_path, reply_id, author_id, expected):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    bridge = SalesTelegram(-100, SalesService(store, Fake()))
    await bridge.ingest(update(text='Подготовь ответ клиенту',
        reply_to_message=NS(message_id=reply_id, from_user=NS(id=author_id))), NS(bot=Bot()))
    assert bool(store.pending_topics()) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('transcript,caption,reply_id,expected', [
    ('Салли, используем Connect Brain 4. Подготовь ответ.', '', 55, True),
    ('Sally, подготовь ответ.', '', 55, True),
    ('Используем Connect Brain 4.', 'Сали, подготовь ответ', 55, True),
    ('Используем Connect Brain 4.', '', 10, True),
    ('Используем Connect Brain 4.', '', 55, False),
    ('Спасибо', '', 10, False),
    ('@another_bot подготовь ответ', '', 10, False),
])
async def test_audio_routing_after_transcription_survives_restart(tmp_path, transcript, caption, reply_id, expected):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    fake = Fake()
    bridge = SalesTelegram(-100, SalesService(store, fake))
    await bridge.ingest(update(text='', caption=caption, voice=NS(file_id='file', file_size=100),
        reply_to_message=NS(message_id=reply_id, from_user=NS(id=8))), NS(bot=Bot()))
    await bridge.service.process(-100, 10)
    assert not fake.contexts and not store.pending_topics()
    store = SalesStore(store.path)
    row = store.awaiting_audio(-100)[0]
    assert store.audio_transcribed(row['event_id'], transcript)
    assert bool(store.pending_topics()) == expected
    assert not store.audio_transcribed(row['event_id'], transcript)
    service = SalesService(store, fake)
    await service.process(-100, 10)
    await service.process(-100, 10)
    assert len(fake.contexts) == int(expected)
    if expected:
        assert transcript in fake.contexts[0]['events'][0]['text']
        assert fake.contexts[0]['topic_id'] == 10


def test_legacy_audio_keeps_existing_routing(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    store.ingest(update_id=1, chat_id=-100, topic_id=10, title='Сделка', message_id=1,
        author_id=7, author='Сергей', date='2026-10-12', text='', source='audio_pending',
        respond=False, audio=dict(file_id='file'))
    assert store.audio_transcribed(1, 'Клиент готов встретиться')
    assert not store.pending_topics()
