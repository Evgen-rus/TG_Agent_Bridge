import asyncio
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from openai_codex import LocalImageInput, TextInput, ApprovalMode, Sandbox

from agentbridge.sales.images import ImageWorker, validate_image, image_suffix
from agentbridge.sales.provider import SalesCodexProvider, SalesFailure, TOOL_FREE
from agentbridge.sales.service import SalesService, validate_result
from agentbridge.sales.storage import SalesStore
from test_sales_buddy import Bot, update, ingest


def png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b''))


EVIDENCE = [dict(kind='readable_text', text='Клиент: Пришлите предложение на маркиратор.'),
            dict(kind='visual_observation', text='На фото конвейер и печатающая головка.'),
            dict(kind='uncertain', text='Цена и модель на табличке не читаются.')]


class Provider:
    def __init__(self):
        self.images, self.contexts = [], []

    async def read_image(self, path, source):
        self.images.append((path, source))
        assert Path(path).read_bytes() == png()
        return EVIDENCE

    async def analyze(self, context):
        self.contexts.append(context)
        images = [e for e in context['events'] if e['source'] == 'image_transcript']
        return dict(answer='Изучены все изображения и вопрос.', basis=[e['id'] for e in context['events']],
            next_action='', goal='', facts=[dict(field='needs', text='Со скриншота: запрос предложения.',
            kind='client_response', event_id=e['id'], quote=EVIDENCE[0]['text'], replaces=0) for e in images])


def remote_bot(data=None, size=None):
    async def download(custom_path):
        Path(custom_path).write_bytes(png() if data is None else data)
    bot = Bot()
    bot.get_file = AsyncMock(return_value=NS(file_size=size, download_to_drive=download))
    return bot


async def send(bridge, bot, number=1, topic=10, caption='', kind='photo', size=100, filename='shot.PNG'):
    media = NS(file_id=f'private-file-{number}', file_size=size, file_name=filename,
               mime_type='image/png', width=100, height=200)
    message = update(topic=topic, text='', caption=caption, **{kind: [media] if kind == 'photo' else media})
    message.update_id = message.effective_message.message_id = number
    await bridge.ingest(message, NS(bot=bot))
    return message


@pytest.mark.asyncio
async def test_album_question_checkpoint_memory_and_topic_isolation(tmp_path):
    from agentbridge.sales.bot import SalesTelegram
    store, provider = SalesStore(tmp_path / 'sales.sqlite3'), Provider()
    service = SalesService(store, provider)
    bridge, bot = SalesTelegram(-100, service), remote_bot()
    message = await send(bridge, bot)
    await bridge.ingest(message, NS(bot=bot))
    # A different update ID for the same Telegram message is also a duplicate.
    message.update_id = 99
    await bridge.ingest(message, NS(bot=bot))
    await send(bridge, bot, 2, kind='document')
    ingest(store, 3, text='Салли, что ответить клиенту?')
    await send(bridge, bot, 4, topic=20)
    with store.connect() as db:
        db.execute('UPDATE events SET received_at=0')
    assert store.pending_topics(batched=True) == []
    await service.process(-100, 10, batch=True)
    assert provider.contexts == []
    # Restart before downloads; metadata is durable and stays outside model context.
    reopened = SalesStore(store.path)
    worker = ImageWorker(SalesService(reopened, provider), tmp_path / 'images')
    await worker.run_once(bot, -100)
    assert len(provider.images) == 3
    assert all('file_id' not in source for _, source in provider.images)
    assert set(reopened.pending_topics(batched=True)) == {(-100, 10), (-100, 20)}
    await service.process(-100, 10, batch=True)
    context = provider.contexts[-1]
    assert [e['id'] for e in context['events']] == [1, 2, 3]
    assert all('private-file' not in json.dumps(e) for e in context['events'])
    assert context['events'][0]['image_evidence'] == EVIDENCE
    assert context['events'][-1]['text'] == 'Салли, что ответить клиенту?'
    assert len(store.context(-100, 10)['facts']) == 2  # Each image retains its own source.
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 4
        assert db.execute('SELECT event_id,quote FROM facts').fetchone()[1] == EVIDENCE[0]['text']
        assert db.execute('SELECT count(*) FROM recommendations').fetchone()[0] == 1
    await worker.run_once(bot, -100)
    assert len(provider.images) == 3
    assert not provider.images[0][0].exists() and not provider.images[1][0].exists()
    assert provider.images[2][0].exists()  # Another deal still pending.
    await service.process(-100, 10, batch=True)
    assert len(provider.contexts) == 1


@pytest.mark.asyncio
async def test_authorization_and_largest_photo_without_size(tmp_path):
    from agentbridge.sales.bot import SalesTelegram
    store, provider = SalesStore(tmp_path / 'sales.sqlite3'), Provider()
    bridge = SalesTelegram(-100, SalesService(store, provider))
    bot = remote_bot()
    bot.get_chat_member = AsyncMock(side_effect=OSError('PRIVATE TOKEN'))
    await send(bridge, bot, size=None)
    worker = ImageWorker(bridge.service, tmp_path / 'images')
    await worker.run_once(bot, -100)
    bot.get_file.assert_not_called()
    await bridge.authorize_pending(Bot())
    await worker.run_once(bot, -100)
    assert len(provider.images) == 1
    message = update(text='', photo=[NS(file_id='small', file_size=None, width=10, height=10),
                                    NS(file_id='large', file_size=None, width=100, height=100)])
    message.update_id = message.effective_message.message_id = 2
    await bridge.ingest(message, NS(bot=Bot()))
    assert store.awaiting_images(-100)[0]['file_id'] == 'large'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['metadata_size', 'remote_size', 'actual_size', 'invalid', 'empty', 'download'])
async def test_failed_images_are_explicit_bounded_and_preserve_question(tmp_path, monkeypatch, mode, caplog):
    from agentbridge.sales.bot import SalesTelegram
    monkeypatch.setattr('agentbridge.sales.images.MAX_DOWNLOAD_BYTES', 200)
    store, provider = SalesStore(tmp_path / 'sales.sqlite3'), Provider()
    bridge = SalesTelegram(-100, SalesService(store, provider))
    data = b'x' * 201 if mode == 'actual_size' else b'fake.exe' if mode == 'invalid' else b'' if mode == 'empty' else None
    bot = remote_bot(data, 201 if mode == 'remote_size' else 100)
    if mode == 'download':
        bot.get_file = AsyncMock(side_effect=OSError('PRIVATE SECRET'))
    await send(bridge, bot, caption='Салли, что видно?', size=201 if mode == 'metadata_size' else 100)
    worker = ImageWorker(bridge.service, tmp_path / 'images')
    for _ in range(5):
        await worker.run_once(bot, -100)
        with store.connect() as db:
            db.execute('UPDATE image_jobs SET retry_at=0')
    assert provider.images == []
    assert not store.awaiting_images(-100)
    with store.connect() as db:
        row = db.execute('SELECT state,attempts FROM image_jobs').fetchone()
        assert row['state'] == 'failed' and row['attempts'] <= 5
        assert db.execute('SELECT count(*) FROM facts').fetchone()[0] == 0
    event = store.context(-100, 10)['events'][0]
    assert event['source'] == 'unsupported_attachment'
    assert 'Салли, что видно?' in event['text'] and 'не прочитано' in event['text']
    assert len(store.deliveries()) <= 2
    assert 'PRIVATE' not in caplog.text and not list((tmp_path / 'images').rglob('*.part'))


@pytest.mark.asyncio
async def test_codex_failure_retry_and_read_checkpoint(tmp_path):
    from agentbridge.sales.bot import SalesTelegram
    store, provider = SalesStore(tmp_path / 'sales.sqlite3'), Provider()
    service = SalesService(store, provider)
    bridge, bot = SalesTelegram(-100, service), remote_bot()
    await send(bridge, bot)
    worker = ImageWorker(service, tmp_path / 'images')
    original = provider.read_image
    provider.read_image = AsyncMock(side_effect=SalesFailure('codex_network'))
    await worker.run_once(bot, -100)
    assert store.op_get('codex_failure')['kind'] == 'codex_network'
    assert not store.context(-100, 10)['facts']
    assert store.awaiting_images(-100)[0]['path']
    store.op_set('codex_failure', None)
    provider.read_image = original
    await worker.run_once(bot, -100)
    bot.get_file.assert_awaited_once()  # Download checkpoint reused.
    await ImageWorker(SalesService(SalesStore(store.path), provider), tmp_path / 'images').run_once(bot, -100)
    assert len(provider.images) == 1  # Reading checkpoint survives restart.


@pytest.mark.parametrize('kind', ['fact', 'agreement', 'client_response', 'manager_action'])
def test_uncertain_and_visual_evidence_cannot_be_confirmed(tmp_path, kind):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, text='Вопрос', source='image_pending', image={'caption': 'Вопрос'})
    store.image_read(1, EVIDENCE)
    context = store.context(-100, 10)
    for item in EVIDENCE[1:]:
        payload = dict(answer='Ответ', basis=[1], next_action='', goal='', facts=[dict(
            field='confirmed', text=item['text'], quote=item['text'], event_id=1, replaces=0, kind=kind)])
        with pytest.raises(ValueError, match='Uncertain image'):
            validate_result(payload, context)
        payload['facts'][0]['kind'] = 'assumption'
        assert validate_result(payload, context)['facts']
    payload['facts'][0].update(quote='Изображение из сообщения', kind='fact')
    with pytest.raises(ValueError, match='current source'):
        validate_result(payload, context)


@pytest.mark.asyncio
async def test_native_image_input_tool_free_ephemeral_and_no_secrets(tmp_path, monkeypatch, caplog):
    calls = []
    class SDK:
        def __init__(self, config): calls.append(('config', config))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def thread_start(self, **kwargs):
            calls.append(('start', kwargs))
            return NS(id='vision', run=run)
    def run(prompt, **kwargs):
        calls.append(('run', prompt, kwargs))
        return NS(error=None, items=[], final_response=json.dumps({'evidence': EVIDENCE}))
    monkeypatch.setattr('agentbridge.sales.provider.Codex', SDK)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'PRIVATE TOKEN')
    provider = SalesCodexProvider(model='fake', reasoning_effort='medium', cwd=tmp_path,
                                 codex_home=tmp_path / 'codex', knowledge='rules')
    path = tmp_path / 'image.png'
    path.write_bytes(png())
    assert await provider.read_image(path, {'event_id': 1, 'caption': 'Вопрос'}) == EVIDENCE
    assert calls[0][1].env['TELEGRAM_BOT_TOKEN'] == ''
    assert tuple(calls[0][1].config_overrides) == TOOL_FREE
    assert calls[1][1]['ephemeral'] and calls[1][1]['sandbox'] == Sandbox.read_only
    assert calls[1][1]['approval_mode'] == ApprovalMode.deny_all
    prompt = calls[2][1]
    assert isinstance(prompt[0], TextInput) and isinstance(prompt[1], LocalImageInput)
    assert prompt[1].path == str(path.resolve())
    assert 'Вопрос' in prompt[0].text and 'PRIVATE' not in caplog.text


def test_image_validation_and_supported_signatures():
    for header, suffix in [(png(), '.png'), (b'\xff\xd8\xffdata', '.jpg'),
                           (b'GIF89adata', '.gif'), (b'RIFFxxxxWEBPdata', '.webp')]:
        assert image_suffix(header) == suffix
    assert not image_suffix(b'executable')
    for bad in [{}, {'evidence': []}, {'evidence': [{'kind': [], 'text': 'x'}]},
                {'evidence': [{'kind': 'uncertain', 'text': ''}]}]:
        with pytest.raises(ValueError):
            validate_image(bad)


def test_image_fact_replacement_uses_manager_caption_and_durable_review(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, text='Обследование платное')
    context = store.context(-100, 10)
    old = dict(field='confirmed', kind='fact', text='Обследование платное', quote='Обследование платное', event_id=1, replaces=0)
    store.complete(-100, 10, context['events'], dict(answer='Сохранено', facts=[old], basis=[1], goal='', next_action=''))
    ingest(store, 2, text='Посмотри скриншот', source='image_pending', image={'caption': 'Посмотри скриншот'})
    text = 'Клиент: Теперь обследование включено в стоимость'
    store.image_read(2, [dict(kind='readable_text', text=text)])
    context = store.context(-100, 10)
    new = {**old, 'text': 'Со скриншота: обследование включено', 'quote': text, 'event_id': 2, 'replaces': 1}
    payload = validate_result(dict(answer='Предложение', facts=[new], basis=[2], goal='', next_action=''),
                              context, allow_fact_reviews=True)
    assert payload['facts'] == [] and payload['fact_reviews'] == [new]
    assert store.complete(-100, 10, context['events'], payload)
    review = next(d for d in store.deliveries() if d['delivery_key'].startswith('fact-review:'))
    store.delivered(review['id'], 500)
    assert store.decide_fact_review(int(review['delivery_key'].split(':')[1]), -100, 10, 500, 7, True) == 'accepted'
    assert store.context(-100, 10)['facts'][0]['event_id'] == 2


def test_disc_uses_only_readable_image_dialogue_and_persists(tmp_path):
    from test_sales_communication import LINES, profile
    from agentbridge.sales.communication import validate_profile_updates
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, text='\n'.join(LINES), source='image_pending', image={'caption': '\n'.join(LINES)})
    store.image_read(1, [dict(kind='visual_observation', text='\n'.join(LINES))])
    context = store.context(-100, 10)
    assert validate_profile_updates([profile()], context)[0]['status'] == 'insufficient_evidence'
    # A different image with genuinely readable dialogue is a usable durable source.
    ingest(store, 2, text='', source='image_pending', image={'caption': ''})
    store.image_read(2, [dict(kind='readable_text', text='\n'.join(LINES))])
    context = store.context(-100, 10)
    profiles = validate_profile_updates([profile(event_id=2)], context)
    assert profiles[0]['status'] == 'supported'
    assert store.complete(-100, 10, context['events'], dict(answer='Сохранено', facts=[], basis=[2],
        goal='', next_action='', communication_profiles=profiles))
    assert SalesStore(store.path).context(-100, 10)['communication_profiles'][0]['status'] == 'supported'
