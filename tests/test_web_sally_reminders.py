from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from agentbridge.sales.storage import SalesStore
from agentbridge.sales.commitments import Commitments
from agentbridge.web.runtime import WebRuntime
from agentbridge.web.sales_reminders import handle_reminder
from agentbridge.web.store import WebError


def setup(tmp_path):
    sales = SalesStore(tmp_path/'sales.sqlite3')
    provider = SimpleNamespace(analyze_commitments=AsyncMock())
    service = SimpleNamespace(store=sales,provider=provider,_lock=asyncio.Lock())
    service.commitments=Commitments(service,-100)
    runtime = WebRuntime(tmp_path,'sally',service,SimpleNamespace(chat_id=-100))
    with sales.connect() as db:
        db.execute("INSERT INTO deals(chat_id,topic_id,title) VALUES (-100,101,'Test')")
    token=runtime.store.invite('sergey',[])
    runtime.store.enroll(token,'safe-password-for-test')
    actor=runtime.store.session(runtime.store.login('sergey','safe-password-for-test','sally','test'),'sally')
    ident=runtime.store.enqueue(actor,'sally','101','a'*20,dict(text='Сали, напомни через 1 час позвонить'))
    job,actor=runtime.store.claim('sally')
    moment=datetime.fromtimestamp(job['created'],timezone.utc)+timedelta(hours=1)
    row=dict(id=0,kind='manager_action',party='Сергей',action='Позвонить',action_key='call',state='active',
             event_id=1,quote=json.loads(job['payload'])['text'],due_date=None,date_basis='none',material='current',
             reason='request',reminder='set',remind_at=moment.isoformat())
    provider.analyze_commitments.return_value={'updates':[row]}
    return runtime,job,actor


@pytest.mark.asyncio
async def test_sally_confirmation_is_durable_atomic_and_replayable(tmp_path):
    runtime,job,actor=setup(tmp_path)
    assert await handle_reminder(runtime,job,actor,json.loads(job['payload']))
    with runtime.service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM commitments').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
    review=runtime.store.history(actor,'sally','101')[0]
    assert review['state']=='review' and review['can_confirm']
    runtime.store.reminder_action(actor,'101',job['id'],review['revision'],'confirm',product='sally')
    job,actor=runtime.store.claim('sally')
    assert await handle_reminder(runtime,job,actor,{})
    assert await handle_reminder(runtime,job,actor,{})
    with runtime.service.store.connect() as db:
        reminder=db.execute('SELECT * FROM commitments').fetchall()
        assert len(reminder)==1 and reminder[0]['remind_at']>datetime.now(timezone.utc).timestamp()
        assert db.execute('SELECT count(*) FROM events').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==1
    runtime.service.commitments.queue_reminders(now=reminder[0]['remind_at']+1)
    with runtime.service.store.connect() as db:
        text=db.execute("SELECT text FROM outbox WHERE delivery_key LIKE 'reminder:%'").fetchone()[0]
        assert 'напоминаю' in text and 'tg://user?id=-' not in text


@pytest.mark.asyncio
async def test_sally_cancel_saves_no_reminder_and_is_owner_bound(tmp_path):
    runtime,job,actor=setup(tmp_path)
    await handle_reminder(runtime,job,actor,json.loads(job['payload']))
    review=runtime.store.history(actor,'sally','101')[0]
    with pytest.raises(WebError):
        runtime.store.reminder_action({**actor,'id':actor['id']+100},'101',job['id'],review['revision'],'confirm',product='sally')
    runtime.store.reminder_action(actor,'101',job['id'],review['revision'],'cancel',product='sally')
    job,actor=runtime.store.claim('sally')
    await handle_reminder(runtime,job,actor,{})
    with runtime.service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM commitments').fetchone()[0]==0
    assert runtime.store.history(actor,'sally','101')[0]['result']=='Напоминание отменено.'


@pytest.mark.asyncio
async def test_sally_restart_restores_confirmation_and_does_not_reconcile_review(tmp_path):
    runtime,job,actor=setup(tmp_path)
    await handle_reminder(runtime,job,actor,json.loads(job['payload']))
    runtime.reconcile_sales()
    review=runtime.store.history(actor,'sally','101')[0]
    assert review['state']=='review'
    runtime.store.reminder_action(actor,'101',job['id'],review['revision'],'confirm',product='sally')
    runtime.store.claim('sally')
    runtime.store.interrupted('sally')
    job,actor=runtime.store.claim('sally')
    await handle_reminder(runtime,job,actor,{})
    assert runtime.store.history(actor,'sally','101')[0]['state']=='done'

@pytest.mark.asyncio
async def test_sally_existing_reminder_keeps_confirmed_action_and_checks_revision(tmp_path):
    runtime,job,actor=setup(tmp_path)
    with runtime.service.store.connect() as db:
        ident=db.execute("""INSERT INTO commitments(chat_id,topic_id,kind,party,action_key,action,state,event_id,quote,updated_at)
            VALUES (-100,101,'manager_action','Сергей','call','Старый звонок','active',5,'old',1)""").lastrowid
    row=runtime.service.provider.analyze_commitments.return_value['updates'][0]
    row.update(id=ident,action='Позвонить Анне')
    await handle_reminder(runtime,job,actor,json.loads(job['payload']))
    review=runtime.store.history(actor,'sally','101')[0]
    assert 'Позвонить Анне' in review['result']
    runtime.store.reminder_action(actor,'101',job['id'],review['revision'],'confirm',product='sally')
    job,actor=runtime.store.claim('sally')
    await handle_reminder(runtime,job,actor,{})
    with runtime.service.store.connect() as db:
        record=db.execute('SELECT * FROM commitments').fetchone()
        assert record['id']==ident and record['action']=='Позвонить Анне'
    runtime.service.commitments.queue_reminders(now=record['remind_at']+1)
    with runtime.service.store.connect() as db:
        text=db.execute("SELECT text FROM outbox WHERE delivery_key LIKE 'reminder:%'").fetchone()[0]
        assert 'Позвонить Анне' in text


@pytest.mark.asyncio
async def test_sally_disabled_confirmation_and_files_preserve_no_reminder(tmp_path):
    runtime,job,actor=setup(tmp_path)
    await handle_reminder(runtime,job,actor,json.loads(job['payload']))
    review=runtime.store.history(actor,'sally','101')[0]
    runtime.store.reminder_action(actor,'101',job['id'],review['revision'],'confirm',product='sally')
    job,actor=runtime.store.claim('sally')
    runtime.service.commitments=None
    with pytest.raises(WebError,match='отключены'):
        await handle_reminder(runtime,job,actor,{})
    with runtime.service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM commitments').fetchone()[0]==0


@pytest.mark.asyncio
async def test_sally_refinement_revises_review_without_saving(tmp_path):
    runtime,job,actor=setup(tmp_path)
    await handle_reminder(runtime,job,actor,json.loads(job['payload']))
    review=runtime.store.history(actor,'sally','101')[0]
    runtime.store.reminder_action(actor,'101',job['id'],review['revision'],'refine','Уточнение задачи',product='sally')
    job,actor=runtime.store.claim('sally')
    await handle_reminder(runtime,job,actor,{})
    revised=runtime.store.history(actor,'sally','101')[0]
    assert revised['revision']==review['revision']+1 and revised['state']=='review'
    with runtime.service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM commitments').fetchone()[0]==0
    with pytest.raises(WebError):
        runtime.store.reminder_action(actor,'101',job['id'],review['revision'],'confirm',product='sally')

@pytest.mark.asyncio
async def test_sally_general_reminder_queues_and_reads_from_sally_only(tmp_path):
    runtime,job,actor=setup(tmp_path)
    job['context']='0'
    with runtime.store.connect() as db:
        db.execute("UPDATE jobs SET context='0' WHERE id=?",(job['id'],))
    with runtime.service.store.connect() as db:
        db.execute("INSERT INTO deals(chat_id,topic_id,title) VALUES (-100,0,'Общее')")
    await handle_reminder(runtime,job,actor,json.loads(job['payload']))
    review=runtime.store.history(actor,'sally','0')[0]
    runtime.store.reminder_action(actor,'0',job['id'],review['revision'],'confirm',product='sally')
    job,actor=runtime.store.claim('sally')
    await handle_reminder(runtime,job,actor,{})
    with runtime.service.store.connect() as db:
        reminder=db.execute('SELECT * FROM commitments').fetchone()
        assert reminder['topic_id']==0
    runtime.service.commitments.queue_reminders(now=reminder['remind_at']+1)
    with runtime.service.store.connect() as db:
        assert db.execute("SELECT topic_id FROM outbox WHERE delivery_key LIKE 'reminder:%'").fetchone()[0]==0
    from agentbridge.web.data import WebData
    data=WebData(runtime.store,None,tmp_path/'missing-rick.sqlite3',runtime.service.store.path,-100)
    assert data.reminder(actor,reminder['id'],'sally')['action']=='Позвонить'
    with runtime.service.store.connect() as db:
        db.execute('UPDATE commitments SET remind_at=? WHERE id=?',(datetime.now(timezone.utc).timestamp()-1,reminder['id']))
    response=data.notifications(actor,'sally',datetime.now(timezone.utc).timestamp()-5)
    assert response['notifications'][0]['context']=='0'
    with pytest.raises(WebError):
        data.reminder({**actor,'role':'rick'},reminder['id'],'sally')
