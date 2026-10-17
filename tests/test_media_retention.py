from datetime import datetime, timezone
from io import BytesIO
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from agentbridge.cleanup_media import cleanup_telegram
from agentbridge.retention import RETENTION_SECONDS, media_lock, safe_path
from agentbridge.web.media import cleanup, upload
from agentbridge.web.store import WebError, WebStore
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.sales.storage import SalesStore


@pytest.fixture
def store(tmp_path):
    return WebStore(tmp_path/'web/access.sqlite3')


def attachment(store, ident, *, days=31, state=None, user=1, size=10, requested=0):
    path = store.path.parent/'media'/f'{ident:032x}.txt'
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'a'*size)
    created = time.time()-days*86400
    os.utime(path,(created,created))
    with store.connect() as db:
        if state:
            db.execute('INSERT INTO jobs(id,user_id,product,context,key,payload,state,result,prepared,created,updated) VALUES (?,?,\'rick\',\'1\',?,\'{}\',?,\'permanent result\',\'permanent transcript\',?,?)',
                       (ident,user,str(ident),state,created,created))
        db.execute('''INSERT INTO media(id,user_id,product,context,filename,kind,mime,path,created,job_id,requested,file_id)
            VALUES (?,?,'rick','1','file.txt','document','text/plain',?,?,?,?, 'telegram-file')''',
                   (str(ident),user,str(path),created,ident if state else None,requested))
    return path


def test_age_idempotency_and_permanent_text(store):
    old = attachment(store,1,state='done')
    young = attachment(store,2,days=29,state='done')
    assert cleanup(store,dry_run=True)['files']==1 and old.exists()
    assert cleanup(store)['files']==1
    assert not old.exists() and young.exists()
    assert cleanup(store)['files']==0
    with store.connect() as db:
        assert db.execute('SELECT removed FROM media WHERE id=\'1\'').fetchone()[0]=='expired'
        assert tuple(db.execute('SELECT result,prepared FROM jobs WHERE id=1').fetchone())==('permanent result','permanent transcript')


@pytest.mark.parametrize('state',['pending','running','waiting','review','uncertain','error'])
def test_unfinished_jobs_protected(store,state):
    path = attachment(store,1,state=state)
    assert cleanup(store)['files']==0 and path.exists()


def test_downloading_is_protected(store):
    path = attachment(store,1,user=0,requested=2)
    assert cleanup(store)['files']==0 and path.exists()


def test_incomplete_delivery_is_protected_even_with_completed_job(store):
    path = attachment(store,1,state='done')
    store.delivery(1,0,state='sending')
    assert cleanup(store)['files']==0 and path.exists()


def test_quota_evicts_oldest_completed_only_and_not_telegram_cache(store,monkeypatch):
    import agentbridge.web.media as media
    assert media.USER_QUOTA==200*1024*1024
    monkeypatch.setattr(media,'USER_QUOTA',35)
    old = attachment(store,1,days=10,state='done')
    younger = attachment(store,2,days=5,state='done')
    active = attachment(store,3,days=20,state='waiting')
    cache = attachment(store,4,user=0,days=20,size=100)
    result = upload(store,{'id':1},'rick','1','new.txt',BytesIO(b'hello world'),11)
    assert result['id'] and not old.exists()
    assert younger.exists() and active.exists() and cache.exists()
    with store.connect() as db:
        assert db.execute('SELECT removed FROM media WHERE id=\'1\'').fetchone()[0]=='quota'


def test_quota_fails_when_only_unfinished_files_remain(store,monkeypatch):
    import agentbridge.web.media as media
    monkeypatch.setattr(media,'USER_QUOTA',15)
    path = attachment(store,1,state='uncertain')
    with pytest.raises(WebError,match='незавершённых'):
        upload(store,{'id':1},'rick','1','new.txt',BytesIO(b'hello world'),11)
    assert path.exists()


def test_one_unlink_failure_and_crash_tombstone_retry(store,monkeypatch):
    failed = attachment(store,1,state='done')
    good = attachment(store,2,state='done')
    unlink = Path.unlink
    def delete(path,*args,**kwargs):
        if path==failed:
            raise PermissionError('fake')
        return unlink(path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'unlink',delete)
        result = cleanup(store)
    assert result==dict(files=1,bytes=10,errors=1)
    assert failed.exists() and not good.exists()
    assert cleanup(store)['files']==1 and not failed.exists()


def test_orphans_known_names_only_and_path_escape(store,tmp_path):
    folder = store.path.parent/'media'
    folder.mkdir()
    old = folder/('a'*32+'.part')
    young = folder/('b'*32+'.part')
    unknown = folder/'notes.txt'
    outside = tmp_path/'private.txt'
    for path in [old,young,unknown,outside]:
        path.write_bytes(b'keep')
        stamp = time.time()-RETENTION_SECONDS-5
        os.utime(path,(stamp,stamp))
    os.utime(young,None)
    assert safe_path(folder/'../../private.txt',folder) is None
    assert cleanup(store)['files']==1
    assert not old.exists() and young.exists() and unknown.exists() and outside.exists()


def test_rick_expiry_preserves_history_and_protects_pending(tmp_path):
    path = tmp_path/'runtime/agentbridge.sqlite3'
    store = ChatThreadStore(path)
    root = path.parent/'media'
    root.mkdir()
    with store._connect() as db:
        for i,state in [(1,'processed'),(2,'pending')]:
            file = root/str(i)/'1_source.pdf'
            file.parent.mkdir()
            file.write_bytes(b'pdf')
            db.execute('''INSERT INTO telegram_messages(update_id,chat_id,message_id,sender_name,telegram_date,text,role,processing_status,
                created_at,media_kind,media_path,telegram_file_id,download_status)
                VALUES (?,?,1,'author','2020-01-01T00:00:00+00:00','permanent text','client',?,
                '2020-01-01T00:00:00+00:00','document',?,'file','available')''',(i,i,state,str(file)))
    result = cleanup_telegram(path,[root],'rick',dry_run=False)
    assert result['files']==1
    assert not (root/'1/1_source.pdf').exists() and (root/'2/1_source.pdf').exists()
    with store._connect() as db:
        rows = db.execute('SELECT text,download_status FROM telegram_messages ORDER BY id').fetchall()
        assert tuple(rows[0])==('permanent text','expired')
        assert rows[1]['download_status']=='available'
    assert cleanup_telegram(path,[root],'rick',dry_run=False)['files']==0


def test_sally_preserves_transcript_and_evidence(tmp_path):
    store = SalesStore(tmp_path/'sales.sqlite3')
    roots = [tmp_path/'audio',tmp_path/'images']
    for root in roots:
        root.mkdir()
    store.ingest(update_id=1,chat_id=-100,topic_id=2,title='deal',message_id=1,author_id=1,author='user',
                 date='2020-01-01T00:00:00+00:00',text='permanent',audio={'file_id':'file','caption':''})
    file = roots[0]/'-100/1_audio_1.bin'
    file.parent.mkdir(); file.write_bytes(b'audio')
    with store.connect() as db:
        db.execute("UPDATE events SET processed=1")
        db.execute("UPDATE audio_jobs SET state='transcribed',path=?,transcript='permanent speech'",(str(file),))
    assert cleanup_telegram(store.path,roots,'sally',dry_run=False)['files']==1
    with store.connect() as db:
        assert db.execute('SELECT transcript FROM audio_jobs').fetchone()[0]=='permanent speech'
        assert db.execute('SELECT text FROM events').fetchone()[0]=='permanent'


@pytest.mark.skipif(os.name!='posix',reason='Production advisory shared locks are Linux flock')
def test_active_owner_reader_protects_cleanup(tmp_path):
    store = ChatThreadStore(tmp_path/'agentbridge.sqlite3')
    with media_lock(tmp_path,shared=True):
        with pytest.raises(BlockingIOError):
            cleanup_telegram(store.database_path,[tmp_path/'media'],'rick',dry_run=False)
