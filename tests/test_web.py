from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from agentbridge.web.store import WebStore, WebError
from agentbridge.web.data import WebData
from agentbridge.web.server import make_server
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.sales.storage import SalesStore


@pytest.fixture
def web(tmp_path,chat_registry):
    access=WebStore(tmp_path/'access.sqlite3')
    rick=ChatThreadStore(tmp_path/'rick.sqlite3')
    sales=SalesStore(tmp_path/'sales.sqlite3')
    with sales.connect() as db:
        db.execute("INSERT INTO deals(chat_id,topic_id,title) VALUES (-100,101,'Visible'),(-100,102,'Hidden')")
        db.execute("INSERT INTO facts(chat_id,topic_id,field,text,kind,event_id,quote) VALUES (-100,101,'needs','Visible fact','fact',1,'source'),(-100,102,'needs','Hidden fact','fact',2,'source')")
    data=WebData(access,chat_registry,rick.path if hasattr(rick,'path') else tmp_path/'rick.sqlite3',tmp_path/'sales.sqlite3',-100)
    return SimpleNamespace(access=access,data=data,sales=sales,rick=rick,root=tmp_path)


def user(store,login,scopes,product):
    token=store.invite(login,scopes)
    store.enroll(token,'safe-password-for-test')
    session=store.login(login,'safe-password-for-test',product,login)
    return store.session(session,product),session


def test_invite_password_single_use_and_product_isolation(web):
    token=web.access.invite('dmitry',['-100123456'])
    with pytest.raises(WebError):
        web.access.enroll(token,'short')
    web.access.enroll(token,'safe-password-for-test')
    with pytest.raises(WebError):
        web.access.enroll(token,'another-safe-password')
    session=web.access.login('dmitry','safe-password-for-test','rick','test')
    assert web.access.session(session,'rick')['role']=='rick'
    with pytest.raises(WebError):
        web.access.session(session,'sally')
    with pytest.raises(WebError):
        web.access.login('dmitry','safe-password-for-test','sally','test')
    web.access.logout(session)
    with pytest.raises(WebError):
        web.access.session(session,'rick')


def test_member_sees_current_and_future_topics_only_in_own_product_and_group(web,chat_registry):
    actor,_=user(web.access,'sergey',['101'],'sally')
    assert {r['id'] for r in web.data.contexts(actor,'sally')}=={'101','102'}
    detail=web.data.detail(actor,'sally','101')
    assert [f['text'] for f in detail['facts']]==['Visible fact']
    assert 'Hidden fact' not in json.dumps(detail)
    with web.sales.connect() as db:
        db.execute("INSERT INTO deals(chat_id,topic_id,title) VALUES (-100,103,'New topic'),(-200,104,'Other group')")
    assert {r['id'] for r in web.data.contexts(actor,'sally')}=={'101','102','103'}
    assert web.data.detail(actor,'sally','103')['title']=='New topic'
    with pytest.raises(WebError):
        web.data.detail(actor,'sally','104')
    with pytest.raises(WebError):
        web.data.contexts(actor,'rick')
    owner,_=user(web.access,'evgen',[],'rick')
    assert len(web.data.contexts(owner,'sally'))==3
    assert web.data.detail(owner,'rick','-100123456')['facts']=={}
    dmitry,session=user(web.access,'dmitry',['obsolete-chat'],'rick')
    assert web.data.detail(dmitry,'rick','-100123456')['title']=='Acme Support'
    chat=chat_registry.all_chats()[0]
    from dataclasses import replace
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chat_registry,'all_chats',lambda:[chat,replace(chat,telegram_chat_id=-100999,name='New client')])
        assert web.data.detail(web.access.session(session,'rick'),'rick','-100999')['title']=='New client'
    with pytest.raises(WebError):
        web.data.contexts(dmitry,'sally')
    with web.access.connect() as db:
        db.execute('UPDATE users SET disabled=1 WHERE id=?',(dmitry['id'],))
    with pytest.raises(WebError):
        web.access.session(session,'rick')


def test_jobs_idempotency_conflicts_restart_and_revoked_acl(web):
    actor,_=user(web.access,'sergey',['101'],'sally')
    job=web.access.enqueue(actor,'sally','101','a'*20,{'text':'question'})
    assert web.access.enqueue(actor,'sally','101','a'*20,{'text':'question'})==job
    with pytest.raises(WebError) as exc:
        web.access.enqueue(actor,'sally','101','a'*20,{'text':'different'})
    assert exc.value.status==409
    with pytest.raises(WebError):
        web.access.enqueue(actor,'rick','102','b'*20,{'text':'question'})
    assert web.access.claim('rick') is None
    claimed,_=web.access.claim('sally')
    assert claimed['id']==job
    reopened=WebStore(web.access.path)
    reopened.interrupted('sally')
    assert reopened.history(actor,'sally','101')[0]['state']=='pending'
    assert reopened.claim('sally')[0]['id']==job
    with reopened.connect() as db:
        db.execute('UPDATE users SET disabled=1 WHERE id=?',(actor['id'],))
    with pytest.raises(WebError):
        reopened.login('sergey','safe-password-for-test','sally','new-ip')


def test_rate_limit(web):
    for _ in range(5):
        with pytest.raises(WebError) as exc:
            web.access.login('nobody','wrong','rick','bad-ip')
        assert exc.value.status==403
    with pytest.raises(WebError) as exc:
        web.access.login('nobody','wrong','rick','bad-ip')
    assert exc.value.status==429


def test_sally_reader_uses_separate_config_without_requiring_its_token(tmp_path,monkeypatch):
    from agentbridge.web.runtime import sales_location
    folder=tmp_path/'runtime/sales_buddy'
    folder.mkdir(parents=True)
    (folder/'sales.sqlite3').touch()
    (folder/'.env').write_text('OWNER_SALES_BADDY_CHAT_ID=-10042\n')
    monkeypatch.setenv('OWNER_SALES_BADDY_CHAT_ID','-999')
    monkeypatch.delenv('AGENTBRIDGE_WEB_SALES_ENV_FILE',raising=False)
    assert sales_location(tmp_path)==(folder/'sales.sqlite3',-10042)


@pytest.fixture
def http(web):
    (web.root/'mobile-concepts/dist').mkdir(parents=True)
    (web.root/'mobile-concepts/dist/index.html').write_text('<html>shell</html>')
    server=make_server(web.root,web.access,web.data,'https://example.test',port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    def request(method,path,body=None,headers=None):
        connection=HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
        connection.request(method,path,body if isinstance(body,bytes) else json.dumps(body) if body is not None else None,headers or {})
        response=connection.getresponse()
        result=response.read()
        connection.close()
        return response.status,dict(response.getheaders()),result
    yield request
    server.shutdown()
    server.server_close()
    thread.join()


def test_http_auth_csrf_origin_cookies_and_no_cache(web,http):
    token=web.access.invite('evgen',[])
    web.access.enroll(token,'safe-password-for-test')
    assert http('GET','/rick/api/contexts')[0]==401
    body={'login':'evgen','password':'safe-password-for-test'}
    assert http('POST','/rick/api/login',body,{'Content-Type':'application/json','Origin':'https://evil.test'})[0]==403
    status,headers,_=http('POST','/rick/api/login',body,{'Content-Type':'application/json','Origin':'https://example.test'})
    assert status==200
    cookie=headers['Set-Cookie']
    assert all(part in cookie for part in ('Secure','HttpOnly','SameSite=Strict','Path=/rick/'))
    auth={'Cookie':cookie.split(';')[0]}
    status,headers,content=http('GET','/rick/api/session',headers=auth)
    assert status==200 and headers['Cache-Control']=='no-store'
    session=json.loads(content)
    assert set(session)=={'name','login','role','csrf'}
    post={**auth,'Content-Type':'application/json','Origin':'https://example.test'}
    assert http('POST','/rick/api/logout',{},post)[0]==403
    assert http('POST','/rick/api/logout',{},dict(post,**{'X-CSRF-Token':session['csrf']}))[0]==200
    assert http('GET','/rick/api/session',headers=auth)[0]==401


def test_http_manifest_static_traversal_and_idempotency(web,http):
    actor,token=user(web.access,'dmitry',['-100123456'],'rick')
    headers={'Cookie':'__Secure-rick_sid='+token,'X-CSRF-Token':actor['csrf'],'Origin':'https://example.test','Content-Type':'application/json','Idempotency-Key':'c'*20}
    body={'context':'-100123456','text':'How to answer?'}
    first=http('POST','/rick/api/ask',body,headers)
    again=http('POST','/rick/api/ask',body,headers)
    assert first[0]==again[0]==202 and first[2]==again[2]
    assert http('GET','/sally/api/contexts',headers=headers)[0]==401
    assert http('GET','/rick/api/detail?context=-999',headers=headers)[0]==404
    assert http('GET','/rick/../../access.sqlite3')[0]==404
    assert http('GET','/rick/%2e%2e/%2e%2e/access.sqlite3')[0]==404
    manifests=[json.loads(http('GET',f'/{p}/manifest.webmanifest')[2]) for p in ('rick','sally')]
    assert manifests[0]['id']!=manifests[1]['id']
    assert manifests[0]['scope']=='/rick/' and manifests[1]['scope']=='/sally/'


@pytest.mark.parametrize('product',['rick','sally'])
def test_install_icons_are_public_pngs_and_product_isolated(web,http,product):
    from io import BytesIO
    import shutil
    from PIL import Image
    source=Path(__file__).resolve().parents[1]/'agentbridge/web/icons'
    shutil.copytree(source,web.root/'agentbridge/web/icons')
    manifest=json.loads(http('GET',f'/{product}/manifest.webmanifest')[2])
    assert {icon['sizes'] for icon in manifest['icons']}=={'192x192','512x512'}
    for icon in manifest['icons']:
        assert icon['src'].startswith(f'/{product}/{product}-v2-')
        status,headers,raw=http('GET',icon['src'])
        assert status==200 and headers['Content-Type']=='image/png'
        with Image.open(BytesIO(raw)) as image:
            assert image.size==tuple(map(int,icon['sizes'].split('x')))
            assert image.mode=='RGB'
    status,_,raw=http('GET',f'/{product}/{product}-v2-180.png')
    assert status==200
    with Image.open(BytesIO(raw)) as image:
        assert image.size==(180,180)
    assert http('GET',f'/{product}/icon.png')[2]==http('GET',f'/{product}/{product}-v2-512.png')[2]
    other='sally' if product=='rick' else 'rick'
    assert http('GET',f'/{other}/{product}-v2-192.png')[0]==404


def test_owner_invites_members_without_selecting_contexts(web,http):
    owner,token=user(web.access,'evgen',[],'rick')
    headers={'Cookie':'__Secure-rick_sid='+token,'X-CSRF-Token':owner['csrf'],
             'Origin':'https://example.test','Content-Type':'application/json'}
    for login,product in [('dmitry','rick'),('sergey','sally')]:
        status,_,raw=http('POST','/rick/api/invite',{'login':login},headers)
        assert status==200
        invite=json.loads(raw)['url'].split('#invite=')[1]
        web.access.enroll(invite,'safe-password-for-test')
        session=web.access.login(login,'safe-password-for-test',product,login)
        actor=web.access.session(session,product)
        assert WebStore.allowed(actor,product,'future-context')
        assert not WebStore.allowed(actor,'sally' if product=='rick' else 'rick')


@pytest.mark.asyncio
async def test_rick_web_reminder_confirmation_refinement_and_duplicate(web,chat_registry):
    import asyncio
    from agentbridge.agents.base import GeneralTaskPlan
    from agentbridge.application import AgentBridgeApplication
    from agentbridge.web.runtime import WebRuntime
    class Owner:
        async def plan_general_task(self,**kwargs):
            return GeneralTaskPlan('plan','Напомнить проверить отчёт','reminder',
                '2099-10-17T04:00:00+00:00' if 'позже' in kwargs['request'] else '',
                '17 октября 11:00','Проверить отчёт')
        async def answer_owner_query(self,**kwargs):
            raise AssertionError('Reminder must not become a plain model answer')
    service=AgentBridgeApplication(chat_registry,web.rick,Owner(),owner_chat_id=-100777)
    runtime=WebRuntime(web.root,'rick',service,None)
    runtime.store=web.access
    owner,_=user(web.access,'evgen',[],'rick')
    member,_=user(web.access,'dmitry',[],'rick')
    job_id=web.access.enqueue(owner,'rick','-100123456','reminder-create-key-1',{'text':'Напомни проверить отчёт'})
    worker=asyncio.create_task(runtime.run())
    async def wait(state,revision=None):
        for _ in range(200):
            row=web.access.history(owner,'rick','-100123456')[0]
            if row['state']==state and (revision is None or row['revision']==revision):
                return row
            await asyncio.sleep(.01)
        raise AssertionError(row)
    try:
        row=await wait('review',1)
        assert row['task_id'] and not row['can_confirm'] and not service.pending_reminders()
        with pytest.raises(WebError):
            web.access.reminder_action(owner,'-100123456',job_id,1,'confirm')
        with pytest.raises(WebError):
            web.access.reminder_action(member,'-100123456',job_id,1,'cancel')
        web.access.reminder_action(owner,'-100123456',job_id,1,'refine','позже, завтра в 11')
        row=await wait('review',2)
        assert row['can_confirm'] and not service.pending_reminders()
        with pytest.raises(WebError):
            web.access.reminder_action(owner,'-100123456',job_id,1,'confirm')
        web.access.reminder_action(owner,'-100123456',job_id,2,'confirm')
        web.access.reminder_action(owner,'-100123456',job_id,2,'confirm')
        row=await wait('done')
        assert 'сохранено' in row['result']
        reminders=service.pending_reminders()
        assert len(reminders)==1 and reminders[0].related_chat_id==-100123456
        assert reminders[0].created_by_name=='Евгений'
        task=web.rick.get_general_task(row['task_id'])
        assert task.status=='done' and task.payload['reminder_id']==reminders[0].id
        web.access.reminder_action(owner,'-100123456',job_id,2,'confirm')
        assert len(service.pending_reminders())==1
        # Recovery after business commit but before Web acknowledgement never recreates it.
        await runtime.reminder_action(dict(id=job_id,task_id=task.id,action='confirm'),owner,chat_registry.get(-100123456))
        assert len(service.pending_reminders())==1
    finally:
        worker.cancel()
        await asyncio.gather(worker,return_exceptions=True)


def test_web_reminder_review_survives_restart_and_cancel_is_durable(web):
    owner,_=user(web.access,'evgen',[],'rick')
    ident=web.access.enqueue(owner,'rick','-100123456','reminder-review-key-1',{'text':'Напомни'})
    web.access.claim('rick')
    web.access.reminder_review(ident,10,'Подтвердите',True)
    reopened=WebStore(web.access.path)
    reopened.interrupted('rick')
    assert reopened.history(owner,'rick','-100123456')[0]['state']=='review'
    reopened.reminder_action(owner,'-100123456',ident,1,'cancel')
    assert reopened.claim('rick')[0]['action']=='cancel'


def test_http_rick_telegram_and_push_routes_are_authenticated(web,http):
    owner,token=user(web.access,'evgen',[],'rick')
    web.data.owner_chat=-100777
    headers={'Cookie':'__Secure-rick_sid='+token,'X-CSRF-Token':owner['csrf'],
             'Origin':'https://example.test','Content-Type':'application/json'}
    assert json.loads(http('GET','/rick/api/telegram',headers=headers)[2])['url']=='https://t.me/c/777/'
    reminder=web.rick.create_reminder(-100777,'2099-10-17T04:00:00Z','Личное напоминание')
    assert json.loads(http('GET',f'/rick/api/reminder?id={reminder}',headers=headers)[2])['action']=='Личное напоминание'
    assert http('GET','/rick/api/reminder?id=999',headers=headers)[0]==404
    member,member_token=user(web.access,'dmitry',[],'rick')
    assert http('GET',f'/rick/api/reminder?id={reminder}',headers={'Cookie':'__Secure-rick_sid='+member_token})[0]==403
    assert http('GET','/rick/api/push-config')[0]==401
    config=json.loads(http('GET','/rick/api/push-config',headers=headers)[2])
    assert config['public_key'] and config['subscribed'] is False
    assert http('POST','/rick/api/push-subscribe',{'subscription':{}},headers)[0]==400
    assert http('POST','/rick/api/presence',{'visible':True,'window_id':'test'},headers)[0]==200
    assert http('POST','/rick/api/presence',{'visible':'yes'},headers)[0]==400
    assert http('POST','/rick/api/presence',{'visible':False},dict(headers,**{'X-CSRF-Token':'wrong'}))[0]==403
    assert json.loads(http('GET','/rick/api/notifications?after=0',headers=headers)[2])['notifications']==[]
    assert http('GET','/rick/api/notifications?after=nan',headers=headers)[0]==400
    assert http('POST','/rick/api/reminder-action',{'context':'-100123456','job_id':1,'revision':1,'action':'confirm'},
                dict(headers,**{'X-CSRF-Token':'wrong'}))[0]==403


def test_http_sally_notifications_and_push_stay_in_product(web,http):
    actor,token=user(web.access,'sergey',[],'sally')
    web.data.sales_chat=-100999
    headers={'Cookie':'__Secure-sally_sid='+token,'X-CSRF-Token':actor['csrf'],
             'Origin':'https://example.test','Content-Type':'application/json'}
    assert http('GET','/sally/api/push-config',headers=headers)[0]==200
    assert json.loads(http('GET','/sally/api/telegram',headers=headers)[2])['url']=='https://t.me/c/999/'
    assert http('POST','/sally/api/presence',{'visible':True},headers)[0]==200
    assert json.loads(http('GET','/sally/api/notifications?after=0',headers=headers)[2])['notifications']==[]
    assert http('GET','/rick/api/push-config',headers=headers)[0]==401


def test_notification_scan_queues_due_reminders_independently_from_telegram(web,chat_registry):
    import time
    from datetime import datetime,timezone
    from agentbridge.application import AgentBridgeApplication
    from agentbridge.web.runtime import WebRuntime
    owner,_=user(web.access,'evgen',[],'rick')
    service=AgentBridgeApplication(chat_registry,web.rick,object(),owner_chat_id=-100777)
    runtime=WebRuntime(web.root,'rick',service,None)
    runtime.store=web.access
    due=datetime.fromtimestamp(time.time()-5,timezone.utc).isoformat()
    reminder=service.create_reminder(due,'Do not expose this text',related_chat_id=-100123456)
    calls=[]
    push=SimpleNamespace(enqueue=lambda *args,**kwargs:calls.append((args,kwargs)))
    runtime.queue_notifications(push)
    assert calls[0][0]==(owner['id'],f'reminder:{reminder}','-100123456','reminder')
    assert 'Do not expose' not in str(calls)
    service.mark_reminder_sent(reminder)
    calls.clear()
    runtime.queue_notifications(push)
    assert calls[0][0][1]==f'reminder:{reminder}'


@pytest.mark.asyncio
@pytest.mark.parametrize('committed',[False,True])
async def test_interrupted_web_confirmation_recovers_with_one_reminder(web,chat_registry,committed):
    import asyncio
    from agentbridge.application import AgentBridgeApplication
    from agentbridge.web.runtime import WebRuntime
    owner,_=user(web.access,'evgen',[],'rick')
    service=AgentBridgeApplication(chat_registry,web.rick,object(),owner_chat_id=-100777)
    runtime=WebRuntime(web.root,'rick',service,None)
    runtime.store=web.access
    job=web.access.enqueue(owner,'rick','-100123456','recover-reminder-key',{'text':'Напомни'})
    web.access.claim('rick')
    task=web.rick.create_general_task(-100777,'Напомни','Завтра','reminder',dict(
        web_user_id=owner['id'],web_job_id=job,related_chat_id=-100123456,
        remind_at_utc='2099-10-17T04:00:00+00:00',reminder_text='Проверить отчёт'))
    web.access.reminder_review(job,task,'Подтвердите',True)
    web.access.reminder_action(owner,'-100123456',job,1,'confirm')
    web.access.claim('rick')
    web.rick.set_general_task_status(task,'confirming','executing')
    if committed:
        web.rick.complete_reminder_task(task)
    web.access.interrupted('rick')
    assert web.access.history(owner,'rick','-100123456')[0]['state']=='pending'
    worker=asyncio.create_task(runtime.run())
    try:
        for _ in range(100):
            row=web.access.history(owner,'rick','-100123456')[0]
            if row['state']=='done':
                break
            await asyncio.sleep(.01)
        assert row['state']=='done' and 'сохранено' in row['result']
        assert len(service.pending_reminders())==1
    finally:
        worker.cancel()
        await asyncio.gather(worker,return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_member_never_uses_owner_provider(web,chat_registry):
    from agentbridge.web.runtime import WebRuntime
    from agentbridge.application import AgentBridgeApplication
    class Provider:
        async def answer_web_question(self,**kwargs):
            assert kwargs['chat_name']=='Acme Support'
            return 'Scoped answer'
    class Owner:
        async def answer_owner_query(self,**kwargs):
            raise AssertionError('Owner provider must not run for members')
    service=AgentBridgeApplication(chat_registry,web.rick,Provider(),owner_provider=Owner())
    runtime=WebRuntime(web.root,'rick',service,None)
    runtime.store=web.access
    actor,_=user(web.access,'dmitry',['-100123456'],'rick')
    job=web.access.enqueue(actor,'rick','-100123456','d'*20,{'text':'Question'})
    import asyncio
    task=asyncio.create_task(runtime.run())
    for _ in range(100):
        history=web.access.history(actor,'rick','-100123456')
        if history[0]['state']=='done':
            break
        await asyncio.sleep(.01)
    task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    assert history[0]['id']==job and history[0]['result']=='Scoped answer'


@pytest.mark.asyncio
async def test_member_sdk_is_tool_free_and_has_no_owner_context(tmp_path,monkeypatch):
    from agentbridge.agents.codex import CodexProvider
    from openai_codex import ApprovalMode
    captures={}
    class SDK:
        def __init__(self,config):
            captures['config']=config
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def thread_start(self,**kwargs):
            captures['thread']=kwargs
            return SimpleNamespace(id='ephemeral')
    secret=tmp_path/'owner.md'
    secret.write_text('OWNER PRIVATE CONTEXT')
    provider=CodexProvider(cwd=tmp_path,owner_context_path=secret)
    monkeypatch.setattr('agentbridge.agents.codex.Codex',SDK)
    def run(thread,prompt,schema,**kwargs):
        assert 'OWNER PRIVATE CONTEXT' not in prompt
        assert json.loads(prompt)['context']=='Only visible chat'
        return {'answer':'Scoped'}
    monkeypatch.setattr(provider,'_run_json',run)
    assert await provider.answer_web_question(question='?',chat_name='Visible',context_pack='Only visible chat')=='Scoped'
    assert captures['thread']['ephemeral'] is True
    assert captures['thread']['approval_mode']==ApprovalMode.deny_all
    assert 'features.shell_tool=false' in captures['config'].config_overrides
    assert 'mcp_servers={}' in captures['config'].config_overrides


@pytest.mark.asyncio
async def test_sally_web_ingest_uses_existing_process_and_deduplicates(web):
    import asyncio
    from agentbridge.sales.service import SalesService
    from agentbridge.web.runtime import WebRuntime
    class Provider:
        calls=0
        async def analyze(self,context):
            self.calls+=1
            return dict(answer='Real pipeline answer',facts=[],next_action='',goal='',basis=[])
    provider=Provider()
    service=SalesService(web.sales,provider)
    runtime=WebRuntime(web.root,'sally',service,SimpleNamespace(chat_id=-100))
    runtime.store=web.access
    actor,_=user(web.access,'sergey',['101'],'sally')
    job_id=web.access.enqueue(actor,'sally','101','s'*20,{'text':'Question'})
    job,actor=web.access.claim('sally')
    class Bot:
        username='sally_bot'
        calls=0
        async def send_message(self,**kwargs):
            self.calls+=1
            return SimpleNamespace(message_id=500)
    bot=Bot()
    runtime.app=SimpleNamespace(bot=bot)
    task=asyncio.create_task(runtime.sales(job,actor,{'text':'Question'}))
    for _ in range(100):
        if web.sales.pending_topics(): break
        await asyncio.sleep(.01)
    await service.process(-100,101)
    await task
    runtime.reconcile_sales()
    await runtime.sales(job,actor,{'text':'Question'})
    assert provider.calls==1
    assert bot.calls==1
    assert web.access.history(actor,'sally','101')[0]['state']=='done'
    assert web.access.history(actor,'sally','101')[0]['id']==job_id
    with web.sales.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE source='web_text'").fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM outbox WHERE chat_id=-100 AND topic_id=101').fetchone()[0]>=1


def test_binary_upload_auth_csrf_scope_private_read_and_mime(web,http):
    from io import BytesIO
    from PIL import Image
    actor,token=user(web.access,'sergey',[],'sally')
    headers={'Cookie':'__Secure-sally_sid='+token,'X-CSRF-Token':actor['csrf'],
             'Origin':'https://example.test','Content-Type':'application/octet-stream'}
    blob=BytesIO();Image.new('RGB',(8,8)).save(blob,format='PNG');raw=blob.getvalue()
    endpoint='/sally/api/upload?context=101&filename=screenshot.png'
    assert http('POST',endpoint,raw,dict(headers,Origin='https://evil.test'))[0]==403
    assert http('POST',endpoint,raw,dict(headers,**{'X-CSRF-Token':'wrong'}))[0]==403
    assert http('POST',endpoint.replace('101','999'),raw,headers)[0]==404
    status,_,body=http('POST',endpoint,raw,headers)
    assert status==201
    media=json.loads(body)
    url='/sally/api/media?context=101&id='+media['id']
    status,response,body=http('GET',url,headers=headers)
    assert status==200 and body==raw
    assert response['Cache-Control']=='no-store' and response['Content-Type']=='image/png'
    assert http('GET',url)[0]==401
    owner,owner_token=user(web.access,'evgen',[],'sally')
    assert http('GET',url,headers={'Cookie':'__Secure-sally_sid='+owner_token})[0]==404
    assert http('GET',url.replace('101','102'),headers=headers)[0]==404
    post=dict(headers,**{'Content-Type':'application/json','Idempotency-Key':'binary-command-key'})
    assert http('POST','/sally/api/ask',{'context':'101','text':'Фото','attachments':[media['id']]},post)[0]==202
    assert http('POST','/sally/api/ask',{'context':'101','text':'Фото','attachments':[media['id']]},post)[0]==202
    with web.access.connect() as db:db.execute('UPDATE media SET event_id=1 WHERE id=?',(media['id'],))
    assert http('GET',url,headers={'Cookie':'__Secure-sally_sid='+owner_token})[0]==200
    with web.access.connect() as db:
        path=db.execute('SELECT path FROM media WHERE id=?',(media['id'],)).fetchone()[0]
        db.execute('UPDATE media SET file_id=? WHERE id=?',('telegram-file',media['id']))
    Path(path).unlink()
    assert http('GET',url,headers=headers)[0]==409
    with web.access.connect() as db:
        assert db.execute('SELECT requested FROM media WHERE id=?',(media['id'],)).fetchone()[0]==1


def test_sally_shared_chronology_authorship_reply_and_context_boundaries(web):
    actor,_=user(web.access,'sergey',[],'sally')
    web.sales.ingest(update_id=1,chat_id=-100,topic_id=101,title='Deal',message_id=20,
        author_id=1,author='Евгений',date='2026-10-16T09:00:00Z',text='Вопрос')
    web.sales.ingest(update_id=2,chat_id=-100,topic_id=101,title='Deal',message_id=21,
        author_id=2,author='Сергей',date='2026-10-16T09:01:00Z',text='Ответ',reply_to=20)
    context=web.sales.context(-100,101)
    web.sales.complete(-100,101,context['events'],dict(answer='Ответ Салли',facts=[],next_action='',goal='',basis=[]))
    with web.sales.connect() as db:db.execute('UPDATE outbox SET telegram_message_id=22')
    detail=web.data.detail(actor,'sally','101')
    assert [m['author'] for m in detail['messages']]==['Евгений','Сергей','Салли']
    assert [m['message_id'] for m in detail['messages']]==[20,21,22]
    web.data.reply_target('101',20)
    with pytest.raises(WebError):web.data.reply_target('102',20)


def test_rick_owner_private_turns_and_member_work_are_separate(web):
    owner,_=user(web.access,'evgen',[],'rick')
    member,_=user(web.access,'dmitry',[],'rick')
    web.access.enqueue(owner,'rick','-100123456','owner-private-key1',{'text':'OWNER PRIVATE'})
    web.access.enqueue(member,'rick','-100123456','member-shared-key1',{'text':'SHARED WORK'})
    assert 'OWNER PRIVATE' not in json.dumps(web.access.history(member,'rick','-100123456'))
    assert len(web.access.history(owner,'rick','-100123456'))==2
