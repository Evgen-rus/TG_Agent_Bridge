import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agentbridge.sales.activity import SalesActivity, TYPING_REFRESH_SECONDS
from agentbridge.sales.audio import AudioWorker
from agentbridge.sales.bot import SalesTelegram
from agentbridge.sales.storage import SalesStore
from agentbridge.sales.service import SalesService


class Bot:
    def __init__(self, fail=False):
        self.actions = []
        self.fail = fail

    async def send_chat_action(self, **kwargs):
        self.actions.append(kwargs)
        if self.fail:
            raise RuntimeError('SECRET private transcript')


def test_default_refresh_interval():
    assert TYPING_REFRESH_SECONDS == 4


def test_topic_scopes_repeat_and_deduplicate(monkeypatch):
    monkeypatch.setattr('agentbridge.sales.activity.TYPING_REFRESH_SECONDS', .01)

    async def scenario():
        activity, bot = SalesActivity(), Bot()
        async with activity.scope(bot, -100, 10):
            task = activity.tasks[(-100, 10)][0]
            async with activity.scope(bot, -100, 10):
                assert activity.tasks[(-100, 10)] == [task, 2]
                async with activity.scope(bot, -100, 20):
                    assert len(activity.tasks) == 2
                    async with asyncio.timeout(1):
                        while any(sum(a['message_thread_id'] == topic for a in bot.actions) < 3
                                  for topic in (10, 20)):
                            await asyncio.sleep(.005)
                assert len(activity.tasks) == 1
            assert activity.tasks[(-100, 10)] == [task, 1]
        assert not activity.tasks
        before = len(bot.actions)
        await asyncio.sleep(.025)
        assert len(bot.actions) == before
        assert sum(a['message_thread_id'] == 10 for a in bot.actions) >= 3
        assert sum(a['message_thread_id'] == 20 for a in bot.actions) >= 3
        assert all(a['chat_id'] == -100 and a['action'] == 'typing' for a in bot.actions)

    asyncio.run(scenario())


def test_typing_failure_nonfatal_and_safe(caplog):
    async def scenario():
        activity, bot = SalesActivity(), Bot(fail=True)
        async with activity.scope(bot, -100, 10):
            assert bot.actions
        assert not activity.tasks

    asyncio.run(scenario())
    assert 'SECRET' not in caplog.text
    assert 'event=sales_typing_failed' in caplog.text


def test_hanging_telegram_does_not_block_work_and_cancel_cleans():
    async def scenario():
        activity = SalesActivity()
        gate = asyncio.Event()
        started = asyncio.Event()

        async def send(**kwargs):
            started.set()
            await gate.wait()

        bot = NS(send_chat_action=send)

        async def work():
            async with activity.scope(bot, -100, 10):
                await asyncio.sleep(100)

        task = asyncio.create_task(work())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not activity.tasks

    asyncio.run(scenario())


def test_adapter_stop_closes_active_and_future_scopes():
    async def scenario():
        service = NS(store=NS(end_run=lambda run: None))
        adapter = SalesTelegram(-100, service)
        bot = Bot()
        async with adapter.activity.scope(bot, -100, 10):
            await adapter.stop(NS(bot_data={}))
            assert not adapter.activity.tasks
        before = len(bot.actions)
        async with adapter.activity.scope(bot, -100, 10):
            await asyncio.sleep(0)
        assert len(bot.actions) == before

    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_audio_download_and_stt_scope_stop_and_backoff(tmp_path, cancel):
    async def scenario():
        store = SalesStore(tmp_path / 'sales.sqlite3')
        store.ingest(update_id=1, chat_id=-100, topic_id=10, title='deal', message_id=20,
            author_id=7, author='Manager', date='2026-10-12', text='', source='audio_pending',
            audio=dict(file_id='file', filename='voice.ogg', file_size=30, kind='voice'))
        activity, bot = SalesActivity(), Bot()

        async def get_file(file_id):
            assert (-100, 10) in activity.tasks

            async def download(custom_path):
                assert (-100, 10) in activity.tasks
                Path(custom_path).write_bytes(b'OggS0000OpusHead')

            return NS(file_size=30, download_to_drive=download)

        bot.get_file = get_file

        async def stt(*args, **kwargs):
            assert (-100, 10) in activity.tasks
            if cancel:
                raise asyncio.CancelledError
            raise TimeoutError('private')

        worker = AudioWorker(store, tmp_path / 'audio', 'key', 'model', transcriber=stt)
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await worker.run_once(bot, -100, activity=lambda c, t: activity.scope(bot, c, t))
        else:
            await worker.run_once(bot, -100, activity=lambda c, t: activity.scope(bot, c, t))
            before = len(bot.actions)
            await worker.run_once(bot, -100, activity=lambda c, t: activity.scope(bot, c, t))
            assert len(bot.actions) == before
        assert not activity.tasks
        assert bot.actions

    asyncio.run(scenario())


def test_analysis_scope_only_actual_provider_and_no_waiting_or_unavailable(tmp_path):
    async def scenario():
        store = SalesStore(tmp_path / 'sales.sqlite3')
        store.ingest(update_id=1, chat_id=-100, topic_id=10, title='deal', message_id=20,
            author_id=7, author='Manager', date='2026-10-12', text='Клиент выбирает оборудование')
        activity, bot = SalesActivity(), Bot()
        started, release = asyncio.Event(), asyncio.Event()

        async def analyze(context):
            assert (-100, 10) in activity.tasks
            started.set()
            await release.wait()
            return dict(answer='Уточните требования клиента.', facts=[], next_action='', goal='', basis=[])

        service = SalesService(store, NS(analyze=analyze))
        scope = lambda c, t: activity.scope(bot, c, t)
        await service._lock.acquire()
        work = asyncio.create_task(service.process(-100, 10, activity=scope))
        await asyncio.sleep(.01)
        assert not bot.actions
        service._lock.release()
        await asyncio.wait_for(started.wait(), 1)
        assert bot.actions
        release.set()
        await work
        assert not activity.tasks
        before = len(bot.actions)
        await service.process(-100, 10, activity=scope)
        assert len(bot.actions) == before
        service.available = lambda: False
        await service.process(-100, 10, activity=scope)
        assert len(bot.actions) == before

    asyncio.run(scenario())
