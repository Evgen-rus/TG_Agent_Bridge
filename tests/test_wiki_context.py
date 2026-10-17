from dataclasses import dataclass, field

import pytest

from agentbridge.agents.base import AgentReply
from agentbridge.application import AgentBridgeApplication
from agentbridge.learning.store import LearningStore
from agentbridge.sales.service import SalesService
from agentbridge.storage.sqlite import ChatThreadStore
from test_sales_threads import SDK, setup
from test_sales_buddy import ingest


def lesson(store, text='Проверять обещание оплаты перед напоминанием', **changes):
    source = dict(source_id=text,version='1',text='Исправление команды',context='10',author='Сергей',date='2026-10-17',url='')
    candidate = dict(title='Обещание оплаты',situation='Напоминание об оплате',behavior=text,
        rationale='Команда уточнила порядок',scope='global',context='',keywords=['платёж','оплата','обещание'],source_ids=[text],**changes)
    return store.commit_batch([source],[candidate])[0]


def test_review_search_version_and_limits(tmp_path):
    store = LearningStore(tmp_path/'db.sqlite3')
    ident = lesson(store)
    assert not store.search('напоминание оплаты',10)
    store.decide(ident,'accept','Команда',expected_version=1)
    assert store.search('платёж',20)[0]['id']==ident
    first,reset,ids = store.context_for_thread('оплата',10)
    assert first and not reset
    store.remember_thread('thread',ids)
    assert store.context_for_thread('оплата',10,'thread')[0]==''
    revised = store.decide(ident,'refine','Сергей',comment='Новое уточнение',expected_version=1,
        changes={'behavior':'Проверять обещание оплаты и согласованный срок'})
    assert store.get(ident)['status']=='active'
    assert revised['status']=='pending'
    assert store.decide(ident,'refine','Сергей',comment='Новое уточнение',expected_version=1,
        changes={'behavior':'Проверять обещание оплаты и согласованный срок'})['id']==revised['id']
    store.decide(revised['id'],'accept','Евгений',expected_version=2)
    assert store.get(ident)['status']=='superseded'
    assert store.context_for_thread('оплата',10,'thread')[1]
    store.decide(revised['id'],'disable','Сергей',expected_version=2)
    assert store.search('оплата',10)==[]
    with pytest.raises(ValueError):
        store.decide(ident,'accept','Команда')
    for number in range(5):
        new = lesson(store,text=f'Проверять оплату {number}')
        store.decide(new,'accept','Команда')
    rows = store.search('оплата',10)
    assert len(rows)==3
    assert len(store.context_for_thread('оплата',10)[0])<=6000


def test_scope_atomicity_and_legacy_preservation(tmp_path,chat_registry,monkeypatch):
    legacy = ChatThreadStore(tmp_path/'rick.sqlite3')
    rec = legacy.create_recommendation(-100123456,'Acme','Client','message','state','reply',1)
    # The old accepted record must remain present after a repeatable migration.
    with legacy._connect() as db:
        db.execute('''INSERT INTO learning_drafts(recommendation_id,author_user_id,author_name,feedback,
            understanding,scope,regenerate_current,status,created_at,updated_at)
            VALUES (?,1,'Owner','Исправление','Объяснение','global',0,'confirmed','2026-10-17','2026-10-17')''',(rec,))
        draft = db.execute('SELECT max(id) FROM learning_drafts').fetchone()[0]
        db.execute('''INSERT INTO learning_rules(telegram_chat_id,chat_name,rule_text,scope,author_user_id,
            author_name,source_draft_id,status,created_at) VALUES (NULL,'Все','Проверять оплату','global',1,'Owner',?,'active','2026-10-17')''',(draft,))
    store = LearningStore(legacy.database_path if hasattr(legacy,'database_path') else tmp_path/'rick.sqlite3')
    store.import_legacy()
    assert len(store.snapshot()['rules'])==1
    assert legacy.active_rule_texts(-100123456)==['Проверять оплату']
    assert not store.pending_notifications()
    legacy.record_experience(telegram_chat_id=None,chat_name='Все',situation='Оплата',
        lesson='Проверять оплату',source_draft_id=draft)
    legacy.undo_latest_rule()
    assert store.search('оплата',-100123456)==[]
    assert store.snapshot()['history'][0]['status']=='disabled'
    monkeypatch.setenv('RICK_WIKI_ENABLED','true')
    app = AgentBridgeApplication(chat_registry,legacy,Provider())
    app.learning = store
    assert 'Проверять оплату' not in app._context_pack(chat_registry.all_chats()[0])
    source = dict(source_id='x',version='1',text='Источник',context='10')
    bad = dict(title='Срок',situation='Нет срока',behavior='Уточни срок',rationale='Исправление',scope='chat',context='20',source_ids=['x'])
    with pytest.raises(ValueError):
        store.commit_batch([source],[bad])
    assert store.sources_processed([('x','1')])==set()


@dataclass
class Provider:
    calls: list = field(default_factory=list)
    lose: bool = False

    async def suggest(self, **kwargs):
        self.calls.append(kwargs)
        ident = kwargs['thread_id'] or f'thread-{len(self.calls)}'
        if self.lose:
            ident, self.lose = 'recovered-thread',False
        return AgentReply(ident,'Состояние','Совет')


@pytest.mark.asyncio
async def test_rick_passes_rule_once_and_restarts_after_disable(tmp_path,chat_registry,monkeypatch):
    monkeypatch.setenv('RICK_WIKI_ENABLED','true')
    store = ChatThreadStore(tmp_path/'rick.sqlite3')
    provider = Provider()
    app = AgentBridgeApplication(chat_registry,store,provider)
    app.learning = LearningStore(tmp_path/'rick.sqlite3')
    ident = lesson(app.learning)
    app.learning.decide(ident,'accept','Команда')
    await app.handle_message(-100123456,'Клиент','Напоминание об оплате')
    assert 'Подтверждённые правила Wiki' in provider.calls[-1]['context_pack']
    await app.handle_message(-100123456,'Клиент','Продолжи напоминание об оплате')
    assert provider.calls[-1]['thread_id']=='thread-1'
    assert 'Подтверждённые правила Wiki' not in provider.calls[-1]['context_pack']
    provider.lose = True
    await app.handle_message(-100123456,'Клиент','Повтори напоминание об оплате')
    assert provider.calls[-1]['thread_id']=='recovered-thread'
    assert 'Подтверждённые правила Wiki' in provider.calls[-1]['context_pack']
    app.learning.decide(ident,'disable','Команда')
    await app.handle_message(-100123456,'Клиент','Ещё напоминание об оплате')
    assert provider.calls[-1]['thread_id'] is None


def test_disabling_wiki_discards_thread_with_injected_rules(tmp_path,monkeypatch):
    from agentbridge.learning.context import prepare
    store = LearningStore(tmp_path/'db.sqlite3')
    ident = lesson(store)
    store.decide(ident,'accept','Команда')
    store.remember_thread('old',[ident])
    monkeypatch.setenv('RICK_WIKI_ENABLED','false')
    assert prepare(store,'rick','оплата','10','old')==('',True,[])
    assert prepare(store,'rick','оплата','10','new')==('',False,[])


@pytest.mark.asyncio
async def test_sally_rule_once_and_reset_from_sqlite_after_disable(setup,monkeypatch):
    monkeypatch.setenv('SALLY_WIKI_ENABLED','true')
    store,factory = setup
    provider = factory()
    provider.learning = LearningStore(store.path)
    ident = lesson(provider.learning)
    provider.learning.decide(ident,'accept','Команда')
    ingest(store,1,text='Напоминание об оплате')
    await SalesService(store,provider).process(-100,10)
    assert 'wiki_rules' in SDK.turns[-1][1]
    first = store.thread(-100,10)['thread_id']
    ingest(store,2,text='Продолжи напоминание об оплате')
    await SalesService(store,provider).process(-100,10)
    assert 'wiki_rules' not in SDK.turns[-1][1]
    assert SDK.resumes==[first]
    provider.learning.decide(ident,'disable','Команда')
    ingest(store,3,text='Ещё напоминание об оплате')
    await SalesService(store,provider).process(-100,10)
    assert store.thread(-100,10)['thread_id']!=first
    assert 'wiki_rules' not in SDK.turns[-1][1]


@pytest.mark.asyncio
async def test_sally_revocation_bypasses_cache_when_current_query_has_no_match(setup,monkeypatch):
    monkeypatch.setenv('SALLY_WIKI_ENABLED','true')
    store,factory = setup
    provider = factory()
    provider.learning = LearningStore(store.path)
    ident = lesson(provider.learning)
    provider.learning.decide(ident,'accept','Команда')
    ingest(store,1,text='Напоминание об оплате')
    await SalesService(store,provider).process(-100,10)
    ingest(store,2,text='Продолжи')
    context = store.context(-100,10)
    await provider.analyze(context)
    before = len(SDK.turns)
    await provider.analyze(context)
    assert len(SDK.turns)==before
    provider.learning.decide(ident,'disable','Команда')
    await provider.analyze(context)
    assert len(SDK.turns)==before+1
