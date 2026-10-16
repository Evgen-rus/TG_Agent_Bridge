"""Web confirmation for Sally's own durable reminder registry."""
from datetime import datetime, timezone
import json
import re
import time

from agentbridge.sales.commitments import LOCAL, TASK, _normalized, _reminder_time, validate_updates
from .store import WebError


def plan_key(job):
    return 'web-reminder:'+str(job['id'])


async def handle_reminder(runtime,job,user,payload):
    service,store = runtime.service,runtime.service.store
    saved = store.op_get(plan_key(job))
    if job.get('task_id') and saved:
        if saved['user_id']!=user['id'] or saved['topic']!=int(job['context']):
            raise WebError(403,'Напоминание недоступно')
        action = job.get('action')
        if saved['state'] in ('done','cancelled'):
            runtime.store.finish(job['id'],'done',saved['result'])
            return True
        if action=='cancel':
            saved.update(state='cancelled',result='Напоминание отменено.')
            store.op_set(plan_key(job),saved)
            runtime.store.finish(job['id'],'done',saved['result'])
            return True
        if action=='confirm':
            if not getattr(service,'commitments',None):
                raise WebError(409,'Напоминания Сали отключены настройкой.')
            row = saved['row']
            if not row or row['reminder']!='set' or datetime.fromisoformat(row['remind_at']).timestamp()<=time.time():
                runtime.store.reminder_review(job['id'],job['id'],'Время уже прошло или не задано. Уточните дату и время.',False)
                return True
            result = confirm(runtime,job,saved,user)
            runtime.store.finish(job['id'],'done',result)
            return True
        if action!='refine':
            raise WebError(409,'Обновите карточку напоминания')
        question = saved['question']+'\nНапомни с уточнением: '+job['refinement']
        base_date = saved['date']
    else:
        question = payload['text']
        attachments = runtime.store.media(user,'sally',job['context'],payload.get('attachments',[]))
        if any(r['kind']!='audio' for r in attachments):
            if re.search(r'(?i)напом|напомин',question):
                raise WebError(409,'Для напоминания отправьте отдельный текст или голосовое сообщение без файлов.')
            return False
        audio = [r for r in attachments if r['kind']=='audio']
        if audio:
            from agentbridge.transcribe import transcribe_audio_file
            from pathlib import Path
            transcripts = store.op_get('web-audio:'+str(job['id']),{})
            for item in audio:
                if item['id'] not in transcripts:
                    transcript = await transcribe_audio_file(Path(item['path']),api_key=runtime.settings.openai_api_key,
                                                             model=runtime.settings.transcription_model)
                    if not transcript or not transcript.strip():
                        raise WebError(422,'Речь не распознана. Запись сохранена.')
                    transcripts[item['id']] = transcript
                    store.op_set('web-audio:'+str(job['id']),transcripts)
                question += '\n'+transcripts[item['id']]
            with runtime.store.connect() as db:
                db.execute('UPDATE jobs SET prepared=? WHERE id=?',(question,job['id']))
        if not re.search(r'(?i)напом|напомин',question) or re.search(r'(?i)не\s+напомин|отмен[а-я]*\s+напомин',question):
            return False
        if not getattr(service,'commitments',None):
            runtime.store.finish(job['id'],'error','Напоминания Сали отключены настройкой.')
            return True
        base_date = datetime.fromtimestamp(job['created'],timezone.utc).isoformat()
    event = dict(id=1,text=question,date=base_date,source='manager_feedback',author=user['name'])
    context = dict(chat_id=runtime.settings.chat_id,topic_id=int(job['context']),events=[event],history=[],
                   commitments=service.commitments.context(runtime.settings.chat_id,int(job['context'])),
                   outgoing_messages=[],now=datetime.now(LOCAL).isoformat(),task=TASK)
    async with service._lock:
        result = await service.provider.analyze_commitments(context)
    rows = validate_updates(result,context)
    rows = [r for r in rows if r['reminder'] in ('set','clarify')]
    if not rows and not saved:
        return False
    row = rows[0] if len(rows)==1 else None
    valid = bool(row and row['reminder']=='set' and _reminder_time(row,event)>time.time())
    if valid:
        label = datetime.fromisoformat(row['remind_at']).astimezone(LOCAL).strftime('%d.%m.%Y в %H:%M')
        text = f"Подтвердите напоминание на {label} (Новосибирск): {row['action']}."
    else:
        text = 'Уточните дату, время по Новосибирску и текст одного напоминания.'
    text += '\nПосле подтверждения уведомление придёт в Telegram и в приложение, если уведомления включены.'
    store.op_set(plan_key(job),dict(user_id=user['id'],topic=int(job['context']),state='review',row=row,
                                   question=question,date=base_date,context=context))
    runtime.store.reminder_review(job['id'],job['id'],text,valid)
    return True


def confirm(runtime,job,saved,user):
    store = runtime.service.store
    row,event = saved['row'],saved['context']['events'][0]
    validate_updates({'updates':[row]},saved['context'])
    moment = _reminder_time(row,event)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = json.loads(db.execute('SELECT value FROM operations WHERE key=?',(plan_key(job),)).fetchone()[0])
        if current['state']=='done':
            return current['result']
        if current['state']!='review':
            raise WebError(409,'Карточка уже обработана')
        if moment<=time.time():
            raise WebError(409,'Время напоминания уже прошло. Уточните его.')
        ident = row['id']
        if not ident:
            duplicate = next((r for r in db.execute("SELECT action_key,party FROM commitments WHERE chat_id=? AND topic_id=? AND state IN ('active','paused')",
                                   (runtime.settings.chat_id,saved['topic'])) if _normalized(r['action_key'])==_normalized(row['action_key']) and _normalized(r['party'])==_normalized(row['party'])),None)
            if duplicate:
                raise WebError(409,'Похожая договорённость уже существует. Повторите запрос, чтобы учесть её.')
        if ident:
            previous = db.execute('SELECT * FROM commitments WHERE id=? AND chat_id=? AND topic_id=?',
                                  (ident,runtime.settings.chat_id,saved['topic'])).fetchone()
            expected = next(r for r in saved['context']['commitments'] if r['id']==ident)
            if not previous or previous['revision']!=expected['revision'] or previous['state']!='active':
                raise WebError(409,'Договорённость изменилась. Отмените карточку и повторите запрос.')
        synthetic = -3_000_000_000_000-job['id']
        event_id = db.execute('''INSERT INTO events(update_id,chat_id,topic_id,message_id,author_id,author,date,text,source,processed)
            VALUES (?,?,?,?,?,?,?,?,?,1)''',(synthetic,runtime.settings.chat_id,saved['topic'],synthetic,-user['id'],
            user['name'],saved['date'],saved['question'],'manager_feedback')).lastrowid
        if ident:
            db.execute('''UPDATE commitments SET action=?,event_id=?,quote=?,remind_at=?,reminder_event_id=?,reminder_sent_at=NULL,
                quiet=0,revision=revision+1,updated_at=? WHERE id=?''',(row['action'],event_id,row['quote'],moment,event_id,time.time(),ident))
        else:
            ident = db.execute('''INSERT INTO commitments(chat_id,topic_id,kind,party,action_key,action,state,event_id,quote,
                updated_at,remind_at,reminder_event_id) VALUES (?,?,?,?,?,?,'active',?,?,?,?,?)''',
                (runtime.settings.chat_id,saved['topic'],row['kind'],row['party'],_normalized(row['action_key']),row['action'],event_id,
                 row['quote'],time.time(),moment,event_id)).lastrowid
        db.execute('INSERT INTO commitment_events VALUES (?)',(event_id,))
        db.execute('INSERT INTO commitment_audit(commitment_id,event_id,at,decision,reason) VALUES (?,?,?,?,?)',
                   (ident,event_id,time.time(),'web_confirmed','explicit_confirmation'))
        label = datetime.fromtimestamp(moment,LOCAL).strftime('%d.%m.%Y в %H:%M')
        result = f"Напоминание #{ident} сохранено на {label} (Новосибирск): {row['action']}."
        store._queue(db,runtime.settings.chat_id,saved['topic'],f'web-reminder-confirm:{job["id"]}',result)
        current.update(state='done',result=result,reminder_id=ident)
        db.execute('UPDATE operations SET value=? WHERE key=?',(json.dumps(current,ensure_ascii=False),plan_key(job)))
        return result


def queue_notifications(runtime,push):
    cutoff = time.time()-86400
    with runtime.store.connect() as db:
        jobs = db.execute("SELECT * FROM jobs WHERE product='sally' AND state IN ('done','review','error','uncertain') AND updated>=?",(cutoff,)).fetchall()
        users = db.execute("SELECT id FROM users WHERE role IN ('owner','sally') AND disabled=0").fetchall()
    for job in jobs:
        push.enqueue(job['user_id'],f"answer:{job['id']}:{job['revision']}:{job['state']}",job['context'],'answer',created_at=job['updated'])
    if not getattr(runtime.service,'commitments',None):
        return
    with runtime.service.store.connect() as db:
        reminders = db.execute("SELECT * FROM commitments WHERE chat_id=? AND state='active' AND quiet=0 AND remind_at BETWEEN ? AND ?",
                               (runtime.settings.chat_id,cutoff,time.time())).fetchall()
    for row in reminders:
        for user in users:
            push.enqueue(user['id'],f"reminder:{row['id']}:{row['remind_at']}",str(row['topic_id']),'reminder',created_at=row['remind_at'])
