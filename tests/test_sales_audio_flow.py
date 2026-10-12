from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import asyncio

import pytest

from agentbridge.sales.bot import SalesTelegram
from agentbridge.sales.service import SalesService
from agentbridge.sales.storage import SalesStore
from test_sales_buddy import Bot, Fake, update


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,filename', [('voice', None), ('audio','call.mp3'), ('audio','call.m4a'), ('document','call.wav')])
async def test_audio_ingest_is_durable_private_and_no_early_model(tmp_path, kind, filename):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    fake = Fake()
    bridge = SalesTelegram(-100, SalesService(store, fake))
    file = NS(file_id='opaque-file-id', file_name=filename, file_size=500, mime_type='audio/ogg', duration=12)
    event = update(text='', caption='Уточни следующий шаг', **{kind:file})
    await bridge.ingest(event, NS(bot=Bot()))
    await bridge.ingest(event, NS(bot=Bot()))
    assert not store.pending_topics()
    assert not store.context(-100, 10)['events']
    await bridge.service.process(-100, 10)
    assert fake.contexts == []
    reopened = SalesStore(store.path)
    row = reopened.awaiting_audio(-100)[0]
    assert row['file_id'] == 'opaque-file-id' and row['caption'] == 'Уточни следующий шаг'
    assert row['chat_id'] == -100 and row['topic_id'] == 10 and row['message_id'] == 1
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1
    assert reopened.audio_transcribed(row['event_id'], 'Клиент подтвердил встречу')
    await bridge.service.process(-100, 10)
    assert len(fake.contexts) == 1
    assert 'Комментарий менеджера:\nУточни следующий шаг' in fake.contexts[0]['events'][0]['text']
    assert 'Распознанная речь:\nКлиент подтвердил встречу' in fake.contexts[0]['events'][0]['text']
    assert 'opaque-file-id' not in str(fake.contexts)
    await bridge.service.process(-100, 10)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM recommendations').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_audio_authorization_hold_is_never_transcribed_until_allowed(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    bridge = SalesTelegram(-100, SalesService(store, Fake()))
    bot = Bot()
    bot.get_chat_member = AsyncMock(side_effect=OSError('PRIVATE key'))
    await bridge.ingest(update(text='', voice=NS(file_id='file', file_size=100)), NS(bot=bot))
    assert not store.awaiting_audio(-100)
    await bridge.authorize_pending(Bot())
    assert len(store.awaiting_audio(-100)) == 1


@pytest.mark.asyncio
async def test_failed_audio_no_facts_no_success_and_bounded_retry(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    fake = Fake()
    bridge = SalesTelegram(-100, SalesService(store, fake))
    await bridge.ingest(update(text='', voice=NS(file_id='file', file_size=100)), NS(bot=Bot()))
    row = store.awaiting_audio(-100)[0]
    for _ in range(5):
        store.audio_failed(row['event_id'], 'audio_transcription_failed')
    assert not store.awaiting_audio(-100) and not store.pending_topics()
    await bridge.service.process(-100, 10)
    assert fake.contexts == []
    with store.connect() as db:
        assert db.execute('SELECT processed FROM events').fetchone()[0] == 3
        assert db.execute('SELECT state FROM audio_jobs').fetchone()[0] == 'failed'
        assert db.execute('SELECT count(*) FROM facts').fetchone()[0] == 0
    assert len(store.deliveries()) == 2
    assert 'не анализировалось' in store.deliveries()[-1]['text']


@pytest.mark.asyncio
async def test_audio_continue_existing_thread_and_long_transcript_isolation(tmp_path, monkeypatch):
    from test_sales_threads import SDK
    from agentbridge.sales.provider import SalesCodexProvider
    from test_sales_buddy import ingest
    SDK.starts, SDK.resumes, SDK.turns = [], [], []
    SDK.failure, SDK.chunk_failure, SDK.lost = None, None, False
    monkeypatch.setattr('agentbridge.sales.provider.Codex', SDK)
    store = SalesStore(tmp_path / 'sales.sqlite3')
    provider = SalesCodexProvider(model='fake', reasoning_effort='medium', cwd=tmp_path,
        codex_home=tmp_path / 'codex', knowledge='rules', store=store)
    service = SalesService(store, provider)
    bridge = SalesTelegram(-100, service)
    ingest(store, text='НАЧАЛО')
    await service.process(-100, 10)
    thread = store.thread(-100, 10)['thread_id']
    message = update(text='', voice=NS(file_id='file', file_size=100))
    message.update_id = message.effective_message.message_id = 2
    await bridge.ingest(message, NS(bot=Bot()))
    audio = store.awaiting_audio(-100)[0]
    transcript = 'а' * 17000 + '\nСЕРЕДИНА\n' + 'б' * 17000 + '\nКОНЕЦ'
    store.audio_transcribed(audio['event_id'], transcript)
    other = update(topic=20, text='', voice=NS(file_id='other', file_size=100))
    other.update_id = other.effective_message.message_id = 3
    await bridge.ingest(other, NS(bot=Bot()))
    await service.process(-100, 10)
    assert store.thread(-100, 10)['thread_id'] == thread and SDK.resumes == [thread]
    assert SDK.turns[-1][1]['events'][0]['fully_read']
    assert all(row['event_id'] != 3 for row in store.context(-100, 10)['facts'])
    with store.connect() as db:
        assert db.execute('SELECT transcript FROM audio_jobs WHERE event_id=2').fetchone()[0] == transcript
        assert db.execute('SELECT count(*) FROM recommendations WHERE topic_id=10').fetchone()[0] == 2


@pytest.mark.asyncio
async def test_slow_stt_does_not_block_ingest_or_other_topics(tmp_path):
    from agentbridge.sales.audio import AudioWorker
    from test_sales_audio import Bot as AudioBot
    store = SalesStore(tmp_path / 'sales.sqlite3')
    bridge = SalesTelegram(-100, SalesService(store, Fake()))
    bot = Bot()
    bot.get_file = AudioBot().get_file
    await bridge.ingest(update(text='', voice=NS(file_id='voice', file_size=30)), NS(bot=bot))
    started, release = asyncio.Event(), asyncio.Event()
    async def stt(*args, **kwargs):
        started.set()
        await release.wait()
        return 'Клиент готов встретиться'
    worker = AudioWorker(store, tmp_path / 'audio', 'fake-key', 'fake-model', transcriber=stt)
    task = asyncio.create_task(worker.run_once(bot, -100))
    await asyncio.wait_for(started.wait(), 2)
    another = update(topic=20, text='Вторая сделка')
    another.update_id = another.effective_message.message_id = 2
    await asyncio.wait_for(bridge.ingest(another, NS(bot=bot)), 2)
    assert (-100, 20) in store.pending_topics() and (-100, 10) not in store.pending_topics()
    release.set()
    await task
    assert store.context(-100, 10)['events'][0]['source'] == 'audio_transcript'
