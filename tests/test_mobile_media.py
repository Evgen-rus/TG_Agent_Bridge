from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from agentbridge.documents import extract_document
from agentbridge.web.media import upload
from agentbridge.web.runtime import WebRuntime
from agentbridge.web.store import WebError, WebStore
from agentbridge.sales.storage import SalesStore, should_respond
from agentbridge.sales.service import SalesService
from agentbridge.sales.bot import SalesTelegram


@pytest.fixture
def setup(tmp_path):
    access=WebStore(tmp_path/'runtime/web/access.sqlite3')
    token=access.invite('sergey',[])
    access.enroll(token,'safe-password-for-tests')
    user=access.session(access.login('sergey','safe-password-for-tests','sally','test'),'sally')
    store=SalesStore(tmp_path/'sales.sqlite3')
    with store.connect() as db:
        db.execute("INSERT INTO deals(chat_id,topic_id,title) VALUES (-100,101,'Deal'),(-100,0,'General')")
    class Provider:
        calls=0
        async def analyze(self,context):
            self.calls+=1
            return dict(answer='Ответ',facts=[],next_action='',goal='',basis=[])
    provider=Provider()
    service=SalesService(store,provider)
    runtime=WebRuntime(tmp_path,'sally',service,SimpleNamespace(chat_id=-100))
    runtime.store=access
    class Bot:
        username='sally_bot'
        calls=[]
        async def send_message(self,**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(message_id=500+len(self.calls))
        async def send_photo(self,**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(message_id=500+len(self.calls),photo=[SimpleNamespace(file_id='photo')])
        async def send_document(self,**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(message_id=500+len(self.calls),document=SimpleNamespace(file_id='doc'))
    bot=Bot()
    runtime.app=SimpleNamespace(bot=bot)
    return SimpleNamespace(root=tmp_path,access=access,user=user,store=store,service=service,runtime=runtime,bot=bot,provider=provider)


def enqueue(s,payload,topic='101'):
    s.access.enqueue(s.user,'sally',topic,'request-key-123456789',payload)
    return s.access.claim('sally')[0]


@pytest.mark.asyncio
async def test_delivery_ack_crash_replay_is_one_message_and_one_analysis(setup):
    s=setup
    payload={'text':'Вопрос'}
    job=enqueue(s,payload)
    original=s.store.ingest
    s.store.ingest=lambda **kwargs: (_ for _ in ()).throw(RuntimeError('crash after Telegram ack'))
    with pytest.raises(RuntimeError):
        await s.runtime.sales(job,s.user,payload)
    s.store.ingest=original
    s.access.interrupted('sally')
    assert s.access.claim('sally')[0]['id']==job['id']
    await s.runtime.sales(job,s.user,payload)
    await s.service.process(-100,101)
    s.runtime.reconcile_sales()
    await s.runtime.sales(job,s.user,payload)
    assert len(s.bot.calls)==1 and s.provider.calls==1
    with s.store.connect() as db:
        row=db.execute('SELECT * FROM events').fetchone()
        assert row['message_id']==501 and row['author']=='Сергей' and row['author_id']==-s.user['id']
    assert s.access.history(s.user,'sally','101')[0]['state']=='done'


@pytest.mark.asyncio
async def test_ambiguous_telegram_send_never_replays_automatically(setup):
    s=setup
    payload={'text':'Вопрос'}
    job=enqueue(s,payload)
    async def timeout(**kwargs):
        s.bot.calls.append(kwargs)
        raise TimeoutError()
    s.bot.send_message=timeout
    await s.runtime.sales(job,s.user,payload)
    s.access.interrupted('sally')
    assert s.access.claim('sally') is None
    await s.runtime.sales(job,s.user,payload)
    assert len(s.bot.calls)==1
    assert s.access.history(s.user,'sally','101')[0]['state']=='uncertain'
    assert not s.store.pending_topics()


@pytest.mark.asyncio
async def test_multiple_images_and_question_share_original_batch(setup):
    from PIL import Image
    s=setup
    blob=BytesIO()
    Image.new('RGB',(10,10)).save(blob,format='PNG')
    raw=blob.getvalue()
    ids=[upload(s.access,s.user,'sally','101',f'screen-{n}.png',BytesIO(raw),len(raw))['id'] for n in range(2)]
    payload={'text':'Салли, что видно?','attachments':ids}
    job=enqueue(s,payload)
    await s.runtime.sales(job,s.user,payload)
    assert len(s.bot.calls)==3
    with s.store.connect() as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0]==3
        assert db.execute('SELECT count(*) FROM image_jobs').fetchone()[0]==2
        assert {json.loads(r[0])['media_group_id'] for r in db.execute('SELECT metadata FROM image_jobs')}=={'web:'+str(job['id'])}
    assert s.access.history(s.user,'sally','101')[0]['state']=='waiting'
    for item in ids:
        with pytest.raises(WebError):
            s.access.enqueue(s.user,'sally','101','another-request-key',{'text':'repeat','attachments':[item]})


@pytest.mark.asyncio
async def test_general_quiet_messages_persist_without_model(setup):
    s=setup
    job=enqueue(s,{'text':'Сергей, договорились'},'0')
    await s.runtime.sales(job,s.user,{'text':'Сергей, договорились'})
    assert s.bot.calls[0]['message_thread_id'] is None
    assert s.access.history(s.user,'sally','0')[0]['state']=='done'
    assert s.provider.calls==0
    assert not should_respond('Привет','sally_bot',general=True)
    assert should_respond('Салли, помоги','sally_bot',general=True)
    assert should_respond('Продолжи','sally_bot',general=True,replied_to_bot=True)
    with s.store.connect() as db:
        assert db.execute('SELECT topic_id,processed FROM events').fetchone()[:]==(0,1)


@pytest.mark.asyncio
@pytest.mark.parametrize('topic,flag,expected',[(None,False,0),(1,True,0),(777,False,0),(101,True,101),(None,True,None)])
async def test_general_telegram_routing_is_confined_to_configured_forum(setup,topic,flag,expected):
    from datetime import datetime,timezone
    s=setup
    bot=SimpleNamespace(id=9,username='sally_bot')
    async def member(*args):return SimpleNamespace(status='member')
    bot.get_chat_member=member
    message=SimpleNamespace(message_thread_id=topic,is_topic_message=flag,message_id=20,text='Привет',date=datetime.now(timezone.utc))
    update=SimpleNamespace(effective_message=message,effective_user=SimpleNamespace(id=5,is_bot=False,full_name='Евгений'),
        effective_chat=SimpleNamespace(id=-100,type='supergroup',is_forum=True),update_id=20)
    adapter=SalesTelegram(-100,s.service)
    await adapter.ingest(update,SimpleNamespace(bot=bot))
    with s.store.connect() as db:
        row=db.execute('SELECT topic_id FROM events').fetchone()
        assert (row[0] if row else None)==expected
    update.effective_chat.id=-200
    update.update_id=21
    await adapter.ingest(update,SimpleNamespace(bot=bot))
    with s.store.connect() as db:
        assert not db.execute('SELECT 1 FROM events WHERE chat_id=-200').fetchone()


def test_upload_content_limits_and_scope(setup):
    s=setup
    with pytest.raises(WebError):
        upload(s.access,s.user,'sally','101','fake.png',BytesIO(b'\x89PNG\r\n\x1a\nBAD'),11)
    with pytest.raises(WebError):
        upload(s.access,s.user,'sally','101','evil.exe',BytesIO(b'MZ'),2)
    with pytest.raises(WebError):
        upload(s.access,s.user,'sally','101','large.txt',BytesIO(),21*1024*1024)
    item=upload(s.access,s.user,'sally','101','notes.txt',BytesIO(b'Hello'),5)
    assert s.access.media(s.user,'sally','101',[item['id']])
    for product,context in [('rick','101'),('sally','102')]:
        with pytest.raises(WebError):s.access.media(s.user,product,context,[item['id']])
    assert not list((s.access.path.parent/'media').glob('*.part'))


def test_documents_txt_docx_table_pdf_no_text_and_zip_bomb(tmp_path):
    txt=tmp_path/'note.txt';txt.write_text('Факты\nСтрока 2',encoding='utf-8')
    assert extract_document(txt)=='Факты\nСтрока 2'
    doc=tmp_path/'table.docx'
    with zipfile.ZipFile(doc,'w') as z:
        z.writestr('word/document.xml','''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Текст</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Цена</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>''')
    assert 'Цена\t100' in extract_document(doc)
    from pypdf import PdfWriter
    pdf=tmp_path/'scan.pdf';writer=PdfWriter();writer.add_blank_page(100,100);writer.write(pdf)
    with pytest.raises(ValueError,match='текстового слоя'):extract_document(pdf)
    with zipfile.ZipFile(doc,'w',compression=zipfile.ZIP_DEFLATED) as z:z.writestr('word/document.xml',b'x'*(11*1024*1024))
    with pytest.raises(ValueError,match='ограничения'):extract_document(doc)


@pytest.mark.asyncio
async def test_remote_media_metadata_can_be_recovered_without_business_duplication(setup):
    s=setup
    s.store.ingest(update_id=1,chat_id=-100,topic_id=101,title='Deal',message_id=12,author_id=5,author='Евгений',
        date='2026-10-16T10:00:00+00:00',text='Скрин',media=dict(file_id='tg-image',filename='screen.png',kind='photo',mime='image/png'))
    await s.runtime.refresh_media()
    with s.access.connect() as db:
        row=db.execute("SELECT * FROM media WHERE id='sally-1'").fetchone()
        assert row['file_id']=='tg-image' and row['event_id']==1 and row['kind']=='image'
        db.execute("UPDATE media SET requested=1 WHERE id='sally-1'")
        path=Path(row['path'])
    async def download(custom_path):
        Path(custom_path).write_bytes(b'cached-test-image')
    async def get_file(file_id):
        assert file_id=='tg-image'
        return SimpleNamespace(file_size=17,download_to_drive=download)
    s.bot.get_file=get_file
    s.runtime._last_media_refresh=0
    await s.runtime.refresh_media()
    assert path.read_bytes()==b'cached-test-image'
    with s.access.connect() as db:
        assert db.execute("SELECT requested FROM media WHERE id='sally-1'").fetchone()[0]==0
    with s.store.connect() as db:assert db.execute('SELECT count(*) FROM events').fetchone()[0]==1


@pytest.mark.asyncio
async def test_expired_telegram_cache_never_downloads_again(setup):
    s = setup
    s.store.ingest(update_id=1,chat_id=-100,topic_id=101,title='Deal',message_id=12,author_id=5,author='User',
        date='2020-01-01T00:00:00+00:00',text='permanent',media=dict(file_id='old',filename='screen.png',kind='photo'))
    async def forbidden(file_id):
        pytest.fail('Expired attachment must not be downloaded')
    s.bot.get_file = forbidden
    await s.runtime.refresh_media()
    with s.access.connect() as db:
        db.execute("UPDATE media SET requested=1")
    s.runtime._last_media_refresh = 0
    await s.runtime.refresh_media()
    with s.access.connect() as db:
        row = db.execute('SELECT removed,requested FROM media').fetchone()
        assert tuple(row)==('expired',0)
    with s.store.connect() as db:
        assert db.execute('SELECT text FROM events').fetchone()[0]=='permanent'


@pytest.mark.asyncio
async def test_rick_uses_shared_transcriber_and_document_parser_without_telegram(setup,monkeypatch):
    s=setup
    s.runtime.product='rick'
    s.runtime.settings=SimpleNamespace(openai_api_key='fake-never-sent',transcription_model='configured-model')
    text=upload(s.access,s.user,'rick','101','notes.txt',BytesIO(b'Confirmed document'),18)
    audio=upload(s.access,s.user,'rick','101','voice.webm',BytesIO(b'\x1a\x45\xdf\xa3webm'+b'x'*20),28)
    seen=[]
    async def transcribe(path,**kwargs):
        assert kwargs['model']=='configured-model'
        seen.append(path)
        return 'Распознанная речь'
    monkeypatch.setattr('agentbridge.transcribe.transcribe_audio_file',transcribe)
    question,images=await s.runtime.prepare_rick({'id':10,'context':'101'},s.user,{'text':'Вопрос','attachments':[text['id'],audio['id']]})
    assert 'Confirmed document' in question and 'Распознанная речь' in question and not images
    assert len(seen)==1 and not s.bot.calls


def test_web_generated_sally_answer_has_ambiguity_guard(setup):
    s=setup
    s.store.ingest(update_id=-1,chat_id=-100,topic_id=101,title='Deal',message_id=21,
        author_id=-1,author='Сергей',date='now',text='Вопрос',source='web_text')
    context=s.store.context(-100,101)
    s.store.complete(-100,101,context['events'],dict(answer='Ответ',facts=[],next_action='',goal='',basis=[]))
    row=s.store.deliveries()[0]
    assert s.store.claim_web_response(row)
    assert not SalesStore(s.store.path).claim_web_response(row)


def scan_pdf(path, pages=2):
    from PIL import Image, ImageDraw
    images = []
    for n in range(pages):
        image = Image.new('RGB', (600,800), 'white')
        ImageDraw.Draw(image).text((40,40),f'Contract page {n+1}: price 150000',fill='black',font_size=30)
        images.append(image)
    images[0].save(path, 'PDF', save_all=True, append_images=images[1:])
    return path.read_bytes()


def test_scanned_pdf_renders_complete_ordered_pages_and_rejects_oversized_scan(tmp_path):
    from agentbridge.documents import document_material
    from PIL import Image
    pdf = tmp_path/'scan.pdf'
    scan_pdf(pdf)
    material = document_material(pdf,tmp_path/'pages')
    assert len(material['pages'])==2
    assert [Path(p).name for p in material['pages']]==['page-001.png','page-002.png']
    with Image.open(material['pages'][0]) as image:
        assert image.width==1200 and image.height==1600
        assert image.convert('L').getextrema()[0]<100
    scan_pdf(pdf,11)
    with pytest.raises(ValueError,match='Разделите PDF'):
        document_material(pdf,tmp_path/'too-many')
    assert not list((tmp_path/'too-many').glob('*.png'))


@pytest.mark.asyncio
async def test_sally_pdf_and_voice_share_durable_batch_and_visual_evidence(setup):
    from agentbridge.sales.images import ImageWorker
    s = setup
    raw = scan_pdf(s.root/'scan.pdf')
    pdf = upload(s.access,s.user,'sally','101','scan.pdf',BytesIO(raw),len(raw))
    audio = upload(s.access,s.user,'sally','101','voice.webm',BytesIO(b'\x1a\x45\xdf\xa3webm'+b'x'*20),28)
    payload = {'text':'Салли, проверь документ','attachments':[pdf['id'],audio['id']]}
    job = enqueue(s,payload)
    await s.runtime.sales(job,s.user,payload)
    with s.store.connect() as db:
        event = db.execute('SELECT event_id FROM audio_jobs').fetchone()
        db.execute('UPDATE deals SET batch_cutoff_id=(SELECT MAX(id) FROM events) WHERE topic_id=101')
    async def download(custom_path):
        Path(custom_path).write_bytes(raw)
    async def get_file(file_id):
        return SimpleNamespace(file_size=len(raw),download_to_drive=download)
    s.bot.get_file=get_file
    seen = []
    async def read_image(path,source):
        assert Path(path).is_file() and Path(path).suffix=='.png'
        seen.append(str(path))
        return [dict(kind='readable_text',text='Цена 150000'),dict(kind='uncertain',text='Подпись неразборчива')]
    s.provider.read_image = read_image
    await ImageWorker(s.service,s.root/'images').run_once(s.bot,-100)
    assert len(seen)==2 and not any(Path(p).exists() for p in seen)
    assert not s.store.pending_topics(batched=True)
    s.store.audio_transcribed(event[0],'Салли, проверь документ')
    await s.service.process(-100,101,batch=True)
    s.runtime.reconcile_sales()
    assert s.provider.calls==1 and len(s.bot.calls)==3
    with s.store.connect() as db:
        evidence=json.loads(db.execute('SELECT payload FROM image_jobs').fetchone()[0])
        assert '[Страница 2]' in evidence[-1]['text'] and evidence[-1]['kind']=='uncertain'
    assert s.access.history(s.user,'sally','101')[0]['state']=='done'


@pytest.mark.asyncio
async def test_rick_scan_keeps_source_and_passes_page_images_without_telegram(setup):
    s=setup
    raw=scan_pdf(s.root/'scan.pdf')
    item=upload(s.access,s.user,'rick','101','scan.pdf',BytesIO(raw),len(raw))
    question,images=await s.runtime.prepare_rick({'id':10,'context':'101'},s.user,
        {'text':'Проверь','attachments':[item['id']]},s.root/'rendered')
    assert len(images)==2 and 'страницы 2' in question
    assert all(Path(i.path).is_file() and i.kind=='image' for i in images)
    assert Path(s.access.media(s.user,'rick','101',[item['id']])[0]['path']).is_file()
    assert not s.bot.calls


def test_mixed_pdf_keeps_text_and_renders_every_page_and_password_is_rejected(tmp_path):
    from agentbridge.documents import document_material
    from pypdf import PdfReader,PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    scan=tmp_path/'scan.pdf';scan_pdf(scan,1)
    writer=PdfWriter();page=writer.add_blank_page(300,400)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 18 Tf 10 350 Td (Text page) Tj ET')
    page[NameObject('/Contents')]=stream
    writer.append(PdfReader(scan));mixed=tmp_path/'mixed.pdf';writer.write(mixed)
    material=document_material(mixed,tmp_path/'pages')
    assert 'Text page' in material['text'] and len(material['pages'])==2
    writer.encrypt('test-password');writer.write(mixed)
    with pytest.raises(ValueError,match='без пароля'):document_material(mixed,tmp_path/'locked')
    assert not list((tmp_path/'locked').glob('*.png'))
