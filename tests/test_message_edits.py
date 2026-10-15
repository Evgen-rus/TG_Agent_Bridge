import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from telegram import Update

from agentbridge.application import AgentBridgeApplication
from agentbridge.sales.bot import SalesTelegram
from agentbridge.sales.service import SalesService
from agentbridge.sales.storage import SalesStore
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.storage.revisions import allow_message_edits
from agentbridge.telegram.bot import create_telegram_application
from test_inbox import FakeProvider, BoomProvider
from test_sales_buddy import Fake, Bot, update
from test_sales_commitments import Commitments, NOW, proposal


def rick_ingest(service, number=11, text='Новая цена 12000', edit=False, **extra):
    return service.ingest_telegram_message(update_id=number, chat_id=-100123456,
        message_id=101, sender_id=5, sender_name='Alice', telegram_date='2026-10-15T09:00:00+00:00',
        text=text, reply_to_message_id=None, is_owner_chat=False, is_edit=edit, **extra)


@pytest.mark.asyncio
async def test_rick_edit_is_durable_after_failure_and_restart(tmp_path, chat_registry):
    store = ChatThreadStore(tmp_path / 'rick.db')
    service = AgentBridgeApplication(chat_registry, store, FakeProvider())
    assert rick_ingest(service, text='Цена 10000')
    await service.process_pending_chat(-100123456)
    assert rick_ingest(service, 12, edit=True)
    assert not rick_ingest(service, 12, edit=True)
    assert not rick_ingest(service, 13)  # A duplicate original message is still ignored.
    failing = AgentBridgeApplication(chat_registry, store, BoomProvider())
    with pytest.raises(RuntimeError):
        await failing.process_pending_chat(-100123456)
    assert [r.update_id for r in store.pending_messages(-100123456)] == [12]
    fake = FakeProvider()
    restarted = AgentBridgeApplication(chat_registry, ChatThreadStore(tmp_path / 'rick.db'), fake)
    await restarted.catch_up()
    assert len(fake.calls) == 1
    assert 'Изменено сообщение #101' in fake.calls[0]['message']
    assert '12000' in fake.calls[0]['message']
    rows = store.recent_messages(-100123456)
    assert len(rows) == 2
    assert {r.message_id for r in rows} == {101}
    assert {r.text for r in rows} == {'Цена 10000', 'Новая цена 12000'}
    assert not store.pending_messages(-100123456)


@pytest.mark.asyncio
async def test_rick_edit_during_model_turn_remains_pending(tmp_path, chat_registry):
    store = ChatThreadStore(tmp_path / 'rick.db')
    entered, release = asyncio.Event(), asyncio.Event()
    class Slow(FakeProvider):
        async def suggest(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().suggest(**kwargs)
    fake = Slow()
    service = AgentBridgeApplication(chat_registry, store, fake)
    rick_ingest(service, text='Цена 10000')
    task = asyncio.create_task(service.process_pending_chat(-100123456))
    await asyncio.wait_for(entered.wait(), 2)
    rick_ingest(service, 12, edit=True)
    release.set()
    await task
    assert len(fake.calls) == 2
    assert '12000' in fake.calls[-1]['message']
    assert not store.pending_messages(-100123456)


@pytest.mark.asyncio
async def test_rick_adapter_captures_edit_and_delivers_owner_only(tmp_path, chat_registry):
    store = ChatThreadStore(tmp_path / 'rick.db')
    fake = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, fake, owner_chat_id=7654321)
    app = create_telegram_application(token='test-token', owner_chat_id=7654321,
        message_service=service, batch_seconds=0)
    bot = Bot()
    payload = dict(update_id=17, edited_message=dict(message_id=101, date=1792054800,
        edit_date=1792054900, text='Новая цена 12000',
        chat=dict(id=-100123456, type='supergroup', title='Acme Support'),
        **{'from': dict(id=5, is_bot=False, first_name='Alice')}))
    event = Update.de_json(payload, app.bot)
    handler = app.handlers[0][0]
    assert handler.check_update(event)
    await handler.callback(event, NS(bot=bot))
    for _ in range(100):
        if bot.sent:
            break
        await asyncio.sleep(.01)
    assert fake.calls and 'Изменено сообщение #101' in fake.calls[0]['message']
    assert bot.sent and {r['chat_id'] for r in bot.sent} == {7654321}
    assert store.recent_messages(-100123456)[0].is_edit


@pytest.mark.asyncio
async def test_sales_edit_keeps_original_and_processes_once(tmp_path):
    store = SalesStore(tmp_path / 'sales.db')
    fake = Fake()
    service = SalesService(store, fake)
    adapter, context = SalesTelegram(-100, service), NS(bot=Bot())
    await adapter.ingest(update(text='Цена 10000'), context)
    await service.process(-100, 10)
    edited = update(text='Цена 12000')
    edited.update_id = 2
    edited.edited_message = edited.effective_message
    await adapter.ingest(edited, context)
    await adapter.ingest(edited, context)
    pending = store.context(-100, 10)['events']
    assert len(pending) == 1 and pending[0]['edit_update_id'] == 2
    assert pending[0]['message_id'] == 1
    store = SalesStore(store.path)
    await SalesService(store, fake).process(-100, 10)
    assert len(fake.contexts) == 2
    assert fake.contexts[-1]['events'][0]['text'] == 'Цена 12000'
    assert fake.contexts[-1]['history'][0]['text'] == 'Цена 10000'
    assert not store.pending_topics()
    assert len(store.deliveries()) == 2


@pytest.mark.asyncio
async def test_sales_edit_during_analysis_does_not_mark_new_version_processed(tmp_path):
    store = SalesStore(tmp_path / 'sales.db')
    fake = Fake()
    adapter = SalesTelegram(-100, SalesService(store, fake))
    context = NS(bot=Bot())
    await adapter.ingest(update(text='Старая цена'), context)
    old = store.context(-100, 10)
    edited = update(text='Новая цена')
    edited.update_id, edited.edited_message = 2, edited.effective_message
    await adapter.ingest(edited, context)
    from test_sales_buddy import result
    assert store.complete(-100, 10, old['events'], result(old))
    assert [e['update_id'] for e in store.context(-100, 10)['events']] == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize('forbidden', ['bot', 'foreign_chat', 'outsider', 'command', 'restart'])
async def test_sales_edits_preserve_access_and_do_not_execute_commands(tmp_path, forbidden):
    store = SalesStore(tmp_path / 'sales.db')
    adapter = SalesTelegram(-100, SalesService(store, Fake()))
    event = update(text={'command': '/help', 'restart': '/restart'}.get(forbidden, 'Новый текст'))
    event.edited_message = event.effective_message
    bot = Bot('left' if forbidden == 'outsider' else 'administrator')
    if forbidden == 'bot': event.effective_user.is_bot = True
    if forbidden == 'foreign_chat': event.effective_chat.id = -200
    await adapter.ingest(event, NS(bot=bot))
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM restarts').fetchone()[0] == 0
    assert not store.deliveries()


@pytest.mark.asyncio
async def test_sales_edited_image_caption_has_own_durable_job(tmp_path):
    store = SalesStore(tmp_path / 'sales.db')
    adapter = SalesTelegram(-100, SalesService(store, Fake()))
    photo = NS(file_id='fake-photo', width=50, height=50, file_size=100)
    event = update(text=None, caption='Исправленная подпись', photo=[photo])
    event.edited_message = event.effective_message
    await adapter.ingest(event, NS(bot=Bot()))
    rows = store.awaiting_images(-100)
    assert len(rows) == 1
    with store.connect() as db:
        row = db.execute('SELECT * FROM events').fetchone()
        assert row['edit_update_id'] == event.update_id and row['source'] == 'image_pending'


def test_commitment_monitor_sees_edit_as_new_source(tmp_path):
    store = SalesStore(tmp_path / 'sales.db')
    engine = Commitments(SalesService(store, Fake()), -100)
    kwargs = dict(chat_id=-100, topic_id=10, title='Deal', message_id=1,
        author_id=7, author='Сергей', date='2026-10-11T10:00:00+00:00', respond=False)
    store.ingest(update_id=1, text='Завтра отправлю КП', **kwargs)
    first = engine.batch(10)
    assert engine.apply(first, {'updates': [proposal(first['events'][0])]}, now=NOW)
    store.ingest(update_id=2, text='КП отправлять не буду', is_edit=True, **kwargs)
    second = engine.batch(10)
    assert second['snapshot'] != first['snapshot']
    assert [e['update_id'] for e in second['events']] == [2]
    assert second['events'][0]['edit_update_id'] == 2
    old = engine.context(-100, 10)[0]
    changed = proposal(second['events'][0])
    changed.update(id=old['id'], state='cancelled', due_date=None, date_basis='none')
    assert engine.apply(second, {'updates': [changed]}, now=NOW)
    assert not engine.context(-100, 10)


@pytest.mark.parametrize('table', ['events', 'telegram_messages'])
def test_legacy_migration_retains_ids_indexes_sequence_and_repeats_safely(tmp_path, table):
    with sqlite3.connect(tmp_path / 'legacy.db') as db:
        auto = 'AUTOINCREMENT' if table == 'telegram_messages' else ''
        db.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY {auto}, update_id INTEGER UNIQUE, chat_id INTEGER, message_id INTEGER, text TEXT, UNIQUE(chat_id,message_id))')
        db.execute(f'CREATE INDEX saved_index ON {table}(chat_id)')
        db.execute(f'INSERT INTO {table} VALUES (5,1,-100,10,?)', ('Русский текст',))
        if auto:
            db.execute(f'INSERT INTO {table} VALUES (99,99,-100,99,?)', ('deleted',))
            db.execute(f'DELETE FROM {table} WHERE id=99')
        allow_message_edits(db, table)
        assert db.execute(f'SELECT * FROM {table}').fetchone() == (5,1,-100,10,'Русский текст',0)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='saved_index'").fetchone()
        db.execute(f'INSERT INTO {table}(update_id,chat_id,message_id,text,edit_update_id) VALUES (2,-100,10,?,2)', ('Правка',))
        allow_message_edits(db, table)
        assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 2
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f'INSERT INTO {table}(update_id,chat_id,message_id,text) VALUES (3,-100,10,?)', ('Duplicate',))
        if auto:
            assert db.execute(f'SELECT max(id) FROM {table}').fetchone()[0] > 99


@pytest.mark.asyncio
async def test_rick_replaced_media_uses_separate_files(tmp_path, chat_registry):
    store = ChatThreadStore(tmp_path / 'rick.db')
    seen = []
    class Inspect(FakeProvider):
        async def suggest(self, **kwargs):
            seen.extend((a.path, Path(a.path).read_bytes()) for a in kwargs['attachments'])
            return await super().suggest(**kwargs)
    service = AgentBridgeApplication(chat_registry, store, Inspect(), owner_chat_id=7654321)
    app = create_telegram_application(token='test-token', owner_chat_id=7654321,
        message_service=service, batch_seconds=.03, media_dir=tmp_path / 'media')
    class DownloadBot(Bot):
        async def get_file(self, file_id):
            async def download_to_drive(custom_path):
                from pathlib import Path
                Path(custom_path).write_bytes(file_id.encode())
            return NS(download_to_drive=download_to_drive)
    bot = DownloadBot()
    for number, kind, file_id in [(1, 'message', 'original'), (2, 'edited_message', 'replacement')]:
        message = dict(message_id=101, date=1792054800,
            chat=dict(id=-100123456, type='supergroup', title='Acme Support'),
            photo=[dict(file_id=file_id, file_unique_id=file_id, width=100, height=100)],
            **{'from': dict(id=5, is_bot=False, first_name='Alice')})
        if kind == 'edited_message': message['edit_date'] = 1792054900
        event = Update.de_json({'update_id': number, kind: message}, app.bot)
        await app.handlers[0][0].callback(event, NS(bot=bot))
    for _ in range(150):
        if bot.sent: break
        await asyncio.sleep(.01)
    assert len(seen) == 2
    assert seen[0][0] != seen[1][0]
    assert {body for _, body in seen} == {b'original', b'replacement'}


def test_rick_voice_edit_remains_eligible_for_transcription(tmp_path, chat_registry):
    store = ChatThreadStore(tmp_path / 'rick.db')
    service = AgentBridgeApplication(chat_registry, store, FakeProvider())
    rick_ingest(service, 12, text='', edit=True, media_kind='voice', telegram_file_id='voice')
    assert len(service.pending_voice_messages(-100123456)) == 1


def test_rick_edited_commands_do_not_repeat_actions():
    from telegram import User
    from telegram.ext import CommandHandler
    app = create_telegram_application(token='test-token', owner_chat_id=7654321,
        message_service=NS())
    app.bot._bot_user = User(id=123, first_name='Rick', is_bot=True, username='rick_bot')
    for handler in app.handlers[0]:
        if not isinstance(handler, CommandHandler):
            continue
        command = next(iter(handler.commands))
        message = dict(message_id=101, date=1792054800, text='/' + command,
            entities=[dict(type='bot_command', offset=0, length=len(command)+1)],
            chat=dict(id=7654321, type='private', first_name='Owner'),
            **{'from': dict(id=7654321, is_bot=False, first_name='Owner')})
        original = Update.de_json(dict(update_id=1, message=message), app.bot)
        edited = Update.de_json(dict(update_id=2, edited_message=message), app.bot)
        assert handler.check_update(original)
        assert not handler.check_update(edited)
