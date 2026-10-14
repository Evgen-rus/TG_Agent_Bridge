import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from agentbridge.sales.bot import MAX_TEXT_FILE_BYTES, SalesTelegram
from agentbridge.sales.storage import SalesStore
from agentbridge.sales import operations


def message(text='', document=None, user=7, topic=10, number=1):
    sender = NS(id=user, is_bot=False, full_name='Manager')
    chat = NS(id=-100, type='supergroup', is_forum=True)
    msg = NS(message_id=number, message_thread_id=topic, is_topic_message=bool(topic),
        text=text, caption=None, document=document, date=datetime.now(timezone.utc),
        reply_to_message=None)
    return NS(update_id=number, effective_message=msg, effective_user=sender, effective_chat=chat)


class Bot:
    id, username = 123, 'sali_bot'

    def __init__(self, status='administrator', payload=b'transcript'):
        self.status, self.payload, self.sent, self.downloads = status, payload, [], 0
        self.fail_delivery = False
        self.fail_member = False
        self.fail_download = False

    async def get_chat_member(self, chat, user):
        if self.fail_member:
            raise RuntimeError('private credential')
        return NS(status=self.status)

    async def get_chat(self, chat):
        return NS(type='supergroup', is_forum=True)

    async def get_file(self, file_id):
        self.downloads += 1
        if self.fail_download:
            raise RuntimeError('private credential')
        return NS(file_size=len(self.payload), download_as_bytearray=AsyncMock(return_value=bytearray(self.payload)))

    async def send_message(self, **kwargs):
        if self.fail_delivery:
            raise RuntimeError('private credential')
        self.sent.append(kwargs)
        return NS(message_id=100 + len(self.sent))


def adapter(tmp_path, bot=None):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    service = NS(store=store, probe=AsyncMock(return_value=True), record_failure=AsyncMock(),
        available=lambda: True, process=AsyncMock(), commitments=None)
    return SalesTelegram(-100, service), bot or Bot()


@pytest.mark.asyncio
async def test_startup_actual_probe_once_and_durable_delivery(tmp_path):
    bridge, bot = adapter(tmp_path)
    bridge.run_id = bridge.service.store.begin_run(-100, 0)
    app = NS(running=True, bot=bot)
    await bridge.startup_probe(app)
    await bridge.startup_probe(app)
    bridge.service.probe.assert_awaited_once()
    assert len(bridge.service.store.deliveries()) == 2
    bot.fail_delivery = True
    await bridge.deliver_once(bot)
    reopened = SalesStore(bridge.service.store.path)
    assert len(reopened.deliveries()) == 2
    bridge.service.store = reopened
    bot.fail_delivery = False
    await bridge.deliver_once(bot)
    assert all(row['message_thread_id'] is None for row in bot.sent)
    assert not reopened.deliveries()
    bridge.run_id = reopened.begin_run(-100, 0)
    await bridge.startup_probe(app)
    assert bridge.service.probe.await_count == 2
    assert any('Точная причина неизвестна' in row['text'] for row in reopened.deliveries())


@pytest.mark.asyncio
async def test_startup_probe_failure_never_claims_ready_or_exposes_secret(tmp_path, caplog):
    bridge, bot = adapter(tmp_path)
    bridge.run_id = bridge.service.store.begin_run(-100, 0)
    bridge.service.probe.side_effect = RuntimeError('sk-private-token')
    await bridge.startup_probe(NS(running=True, bot=bot))
    rows = bridge.service.store.deliveries()
    assert any('недоступен' in row['text'] for row in rows)
    assert all('готова к работе' not in row['text'] and 'private' not in row['text'] for row in rows)
    assert 'private' not in caplog.text


@pytest.mark.asyncio
async def test_restart_admin_confirmation_dedup_and_authorization(tmp_path, monkeypatch):
    bridge, bot = adapter(tmp_path)
    restart = []
    bridge.restart_verify_seconds = 0
    monkeypatch.setattr('agentbridge.sales.bot.restart_sales', lambda: restart.append('sales-buddy.service'))
    event = message('/restart', topic=None)
    bot.status = 'member'
    await bridge.ingest(event, NS(bot=bot))
    assert not bridge.service.store.deliveries()
    bot.status = 'administrator'
    await bridge.ingest(event, NS(bot=bot))
    await bridge.ingest(event, NS(bot=bot))
    assert len(bridge.service.store.deliveries()) == 1
    await bridge.deliver_once(bot)
    button = bot.sent[0]['reply_markup'].inline_keyboard[0][0]
    query = NS(data=button.callback_data, answer=AsyncMock(), message=NS(message_thread_id=None, message_id=101))
    click = NS(callback_query=query, effective_user=event.effective_user, effective_chat=event.effective_chat)
    bot.status = 'member'
    await bridge.callback(click, NS(bot=bot))
    assert not restart
    bot.status = 'administrator'
    await bridge.callback(click, NS(bot=bot))
    await asyncio.gather(*bridge.restart_tasks)
    await bridge.callback(click, NS(bot=bot))
    assert restart == ['sales-buddy.service']
    assert any('Запускаю подтверждённый' in item['text'] for item in bot.sent)


@pytest.mark.asyncio
async def test_restart_error_is_safe_and_service_stays_running(tmp_path, monkeypatch, caplog):
    bridge, bot = adapter(tmp_path)
    marker = bridge.service.store.request_restart(-100, 10, 7, 1)
    assert bridge.service.store.claim_restart(marker, 7)
    def fail():
        raise RuntimeError('secret-token')
    monkeypatch.setattr('agentbridge.sales.bot.restart_sales', fail)
    await bridge.launch_restart(marker)
    assert bridge.service.store.get_restart(marker)['status'] == 'failed'
    assert any('не удался' in row['text'] for row in bridge.service.store.deliveries())
    assert 'secret-token' not in caplog.text


@pytest.mark.asyncio
async def test_restart_accepted_but_old_process_remains_reports_uncertainty(tmp_path, monkeypatch):
    bridge, bot = adapter(tmp_path)
    marker = bridge.service.store.request_restart(-100, 10, 7, 1)
    assert bridge.service.store.claim_restart(marker, 7)
    bridge.restart_verify_seconds = 0
    observed = []
    monkeypatch.setattr('agentbridge.sales.bot.restart_sales', lambda: observed.append(
        bridge.service.store.get_restart(marker)['status']))
    await bridge.launch_restart(marker)
    assert observed == ['launched']
    assert bridge.service.store.get_restart(marker)['status'] == 'failed'
    assert any('не подтверждена' in row['text'] for row in bridge.service.store.deliveries())


@pytest.mark.asyncio
async def test_callback_requires_exact_confirmation_message_topic_and_requester(tmp_path, monkeypatch):
    bridge, bot = adapter(tmp_path)
    restart = []
    monkeypatch.setattr('agentbridge.sales.bot.restart_sales', lambda: restart.append(True))
    marker = bridge.service.store.request_restart(-100, 10, 7, 1)
    await bridge.deliver_once(bot)
    query = NS(data=f'sales_restart:{marker}', answer=AsyncMock(), message=NS(message_thread_id=10, message_id=101))
    original = message(user=8)
    click = NS(callback_query=query, effective_user=original.effective_user, effective_chat=original.effective_chat)
    await bridge.callback(click, NS(bot=bot))
    click.effective_user.id = 7
    query.message.message_thread_id = 20
    await bridge.callback(click, NS(bot=bot))
    query.message.message_thread_id = 10
    query.message.message_id = 999
    await bridge.callback(click, NS(bot=bot))
    query.message.message_id = 101
    click.effective_chat.id = -200
    await bridge.callback(click, NS(bot=bot))
    assert not restart and bridge.service.store.get_restart(marker)['status'] == 'pending'


@pytest.mark.asyncio
async def test_start_stop_reuses_polling_watchdog_and_clean_marker(tmp_path, monkeypatch):
    bridge, bot = adapter(tmp_path)
    watchdogs = []
    class Watchdog:
        def __init__(self, *args, **kwargs):
            watchdogs.append((args, kwargs))
        async def run(self, app):
            await asyncio.Event().wait()
    monkeypatch.setattr('agentbridge.sales.bot.PollingWatchdog', Watchdog)
    app = NS(bot=bot, running=True, bot_data={}, updater=NS(running=True))
    await bridge.start(app)
    await asyncio.sleep(.05)
    await bridge.stop(app)
    args, kwargs = watchdogs[0]
    assert args[0] is bridge.heartbeat
    assert args[4] == ['message', 'callback_query']
    assert kwargs['fatal_state_key'] == 'sales_polling_fatal'
    assert bridge.service.store.op_get('run')['clean'] is True
    count = len(bridge.service.store.deliveries())
    bridge.service.store.begin_run(-100, 0)
    assert len(bridge.service.store.deliveries()) == count


@pytest.mark.asyncio
async def test_text_file_full_body_persisted_and_duplicate_no_download(tmp_path):
    body = ('Начало\n' + 'Важная середина\n' * 2000 + 'Клиент согласовал финал').encode('utf-8')
    bridge, bot = adapter(tmp_path, Bot(payload=body))
    event = message(document=NS(file_id='file', file_name='call.md', file_size=len(body)))
    await bridge.ingest(event, NS(bot=bot))
    assert not bridge.service.store.pending_topics()
    bridge.service.store = SalesStore(bridge.service.store.path)
    await bridge.hydrate_documents(bot)
    await bridge.ingest(event, NS(bot=bot))
    await bridge.hydrate_documents(bot)
    assert bot.downloads == 1
    assert bridge.service.store.op_get('document-source:1')['filename'] == 'call.md'
    with bridge.service.store.connect() as db:
        row = db.execute('SELECT * FROM events').fetchone()
        assert row['text'] == body.decode('utf-8') and row['source'] == 'text_document'
    assert bridge.service.store.pending_topics() == [(-100, 10)]


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,size', [(b'\xff\xfe', 2), (b'text', MAX_TEXT_FILE_BYTES + 1)])
async def test_file_size_and_utf8_errors_explicit(tmp_path, payload, size):
    bridge, bot = adapter(tmp_path, Bot(payload=payload))
    await bridge.ingest(message(document=NS(file_id='file', file_name='call.txt', file_size=size)), NS(bot=bot))
    await bridge.hydrate_documents(bot)
    with bridge.service.store.connect() as db:
        row = db.execute('SELECT * FROM events').fetchone()
        assert row['source'] == 'unsupported_attachment' and 'не прочитан' in row['text']
    if size > MAX_TEXT_FILE_BYTES:
        assert bot.downloads == 0


@pytest.mark.asyncio
async def test_file_temporary_authorization_and_download_retry_survive_restart(tmp_path, caplog):
    bridge, bot = adapter(tmp_path)
    event = message(document=NS(file_id='file', file_name='call.txt', file_size=10))
    bot.fail_member = True
    await bridge.ingest(event, NS(bot=bot))
    await bridge.hydrate_documents(bot)
    assert not bot.downloads
    bridge.service.store = SalesStore(bridge.service.store.path)
    bot.fail_member = False
    await bridge.authorize_pending(bot)
    bot.fail_download = True
    await bridge.hydrate_documents(bot)
    assert bridge.service.store.awaiting_documents(-100)
    bot.fail_download = False
    bridge.service.store.op_set('document-retry:1', 0)
    await bridge.hydrate_documents(bot)
    assert not bridge.service.store.awaiting_documents(-100)
    assert 'private credential' not in caplog.text


def test_fixed_restart_command_cannot_touch_rick(monkeypatch):
    monkeypatch.setattr(operations, 'self_restart_supported', lambda: True)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return NS(returncode=0)
    monkeypatch.setattr(operations.subprocess, 'run', run)
    operations.restart_sales()
    assert calls == [['/usr/bin/sudo', '-n', '/usr/bin/systemctl', '--no-block', 'restart', 'sales-buddy.service']]


@pytest.mark.parametrize('status,failed', [(0, False), (-15, False), (1, True)])
def test_fixed_restart_rejection_and_systemd_kill(monkeypatch, status, failed):
    monkeypatch.setattr(operations, 'self_restart_supported', lambda: True)
    monkeypatch.setattr(operations, 'process_is_going_away', lambda _: False)
    monkeypatch.setattr(operations.subprocess, 'run', lambda *args, **kwargs: NS(returncode=status))
    if failed:
        with pytest.raises(RuntimeError):
            operations.restart_sales()
    else:
        operations.restart_sales()


def test_windows_restart_is_refused_without_subprocess(monkeypatch):
    monkeypatch.setattr(operations, 'self_restart_supported', lambda: False)
    monkeypatch.setattr(operations.subprocess, 'run', lambda *args, **kwargs: pytest.fail('No subprocess on unsupported platform'))
    with pytest.raises(RuntimeError):
        operations.restart_sales()
