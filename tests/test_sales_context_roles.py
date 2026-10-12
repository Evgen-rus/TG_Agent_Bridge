from copy import deepcopy

import pytest

from agentbridge.sales.communication import validate_profile_updates, DISC_INSTRUCTIONS
from agentbridge.sales.storage import SalesStore
from test_sales_communication import profile, ingest, complete
from test_sales_editing import setup

REPLIES = ['На нашем производстве нужно сравнить совместимость двух моделей и технические риски.',
           'Прежде чем принимать решение, пришлите подробные критерии и документы по оборудованию.']
DIALOGUE = ('Добрый день, Сергей, это Иван по оборудованию. Какие критерии вам важны?\n' +
            REPLIES[0] + '\nМогу прислать расчёт и организовать проверку. Что сравнить?\n' + REPLIES[1])


def attributed(contact='client@1', event_id=1, text=DIALOGUE):
    value = profile(contact, event_id)
    value['evidence'] = [dict(event_id=event_id, role_quote=reply, quote=reply,
        observation='Клиент предпочитает проверяемые технические обоснования.',
        attribution=dict(role='client', material='dialogue', client_count=1, confidence='high',
            reason='Отвечает на вопросы продавца о собственном производстве и критериях выбора.',
            context_quote=text, identity_quote='' if contact.startswith('client@') else
                'Добрый день, Сергей, это Иван по оборудованию.')) for reply in REPLIES]
    return value


def ctx(text=DIALOGUE, source='text_document', event_id=1):
    return dict(events=[dict(id=event_id, text=text, source=source)], history=[], communication_profiles=[])


def test_unlabelled_dialogue_and_anonymous_source_scoping():
    result = validate_profile_updates([attributed()], ctx())[0]
    assert result['status'] == 'supported' and result['contact_id'] == 'client@1'
    assert len(result['evidence']) == 2
    assert all(e['attribution']['reason'] for e in result['evidence'])
    wrong = attributed('client@2')
    assert validate_profile_updates([wrong], ctx())[0]['status'] == 'insufficient_evidence'


def test_client_sergey_not_manager_by_name():
    value = attributed('Сергей')
    validated = validate_profile_updates([value], ctx())[0]
    assert validated['status'] == 'supported'
    assert validated['contact_id'] == 'Сергей'


@pytest.mark.parametrize('field,value', [('role', 'manager'), ('role', 'unknown'),
    ('material', 'manager_summary'), ('material', 'unknown'), ('confidence', 'low'),
    ('context_quote', 'Вымышленный разговор о другом клиенте.'), ('identity_quote', 'Привет, Сергей из другой записи.')])
def test_unreliable_attribution_neutralizes_profile(field, value):
    proposed = attributed('Сергей')
    for evidence in proposed['evidence']:
        evidence['attribution'][field] = value
    assert validate_profile_updates([proposed], ctx())[0]['status'] == 'insufficient_evidence'


def test_manager_voice_summary_cannot_be_direct_client_evidence():
    text = 'Сали, пересказываю разговор. Клиенту важны документы. Я предложил выезд.'
    proposed = attributed()
    proposed['evidence'] = [dict(proposed['evidence'][0], role_quote=text, quote=text)]
    proposed['evidence'][0]['attribution'].update(material='manager_summary', context_quote=text)
    source = ctx('Распознанная речь:\n' + text, 'audio_transcript')
    source['events'][0]['speech_offset'] = len('Распознанная речь:\n')
    assert validate_profile_updates([proposed], source)[0]['status'] == 'insufficient_evidence'


def test_audio_caption_excluded_context_dialogue_accepted():
    proposed = attributed()
    source = ctx('Комментарий менеджера:\nМоя версия\n\nРаспознанная речь:\n' + DIALOGUE, 'audio_transcript')
    source['events'][0]['speech_offset'] = source['events'][0]['text'].index(DIALOGUE)
    assert validate_profile_updates([proposed], source)[0]['status'] == 'supported'
    for evidence in proposed['evidence']:
        evidence['attribution']['context_quote'] = source['events'][0]['text']
    assert validate_profile_updates([proposed], source)[0]['status'] == 'insufficient_evidence'


def test_explicit_manager_label_cannot_be_overridden_or_removed():
    for with_label in (True, False):
        text = '\n'.join('Менеджер Сергей: ' + r for r in REPLIES) + '\nКлиент: Спасибо.'
        proposed = attributed(text=text)
        for item in proposed['evidence']:
            if with_label:
                item['role_quote'] = 'Менеджер Сергей: ' + item['quote']
        assert validate_profile_updates([proposed], ctx(text))[0]['status'] == 'insufficient_evidence'


def test_two_unnamed_calls_do_not_merge_and_restart_keeps_reasons(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, text=DIALOGUE)
    complete(store, [attributed()])
    ingest(store, 2, text=DIALOGUE)
    complete(store, [attributed('client@2', 2)])
    reopened = SalesStore(store.path)
    profiles = reopened.context(-100, 10)['communication_profiles']
    assert {p['contact_id'] for p in profiles} == {'client@1', 'client@2'}
    assert all(p['evidence'][0]['attribution']['confidence'] == 'high' for p in profiles)
    ingest(reopened, 3, topic=20, text=DIALOGUE)
    assert reopened.context(-100, 20)['communication_profiles'] == []


def test_manager_correction_invalidates_context_attribution_and_keeps_revision(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store, text=DIALOGUE)
    complete(store, [attributed()])
    correction = 'Исправляю: это говорил менеджер, а не клиент из первого разговора.'
    ingest(store, 2, text=correction, source='manager_feedback')
    proposed = attributed()
    proposed['correction'] = dict(event_id=2, quote=correction, invalidated_event_ids=[1])
    complete(store, [proposed])
    saved = store.context(-100, 10)['communication_profiles'][0]
    assert saved['status'] == 'insufficient_evidence'
    assert len(store.profile_history(-100, 10, 'client@1')) == 2


def test_ambiguous_roles_single_observation_and_schema_contract():
    proposed = attributed()
    proposed['evidence'] = proposed['evidence'][:1]
    assert validate_profile_updates([proposed], ctx())[0]['status'] == 'tentative'
    proposed['role_separation_confidence'] = 'low'
    assert validate_profile_updates([proposed], ctx())[0]['status'] == 'insufficient_evidence'
    assert 'Подписи не обязательны' in DISC_INSTRUCTIONS
    assert 'Сергей может быть и клиентом' in DISC_INSTRUCTIONS


def test_identity_is_required_for_named_unlabelled_profile():
    proposed = attributed('Сергей')
    for item in proposed['evidence']:
        item['attribution']['identity_quote'] = ''
    assert validate_profile_updates([proposed], ctx())[0]['status'] == 'insufficient_evidence'


@pytest.mark.parametrize('count', [0, 2])
def test_unknown_or_multiple_clients_cannot_share_anonymous_profile(count):
    proposed = attributed()
    for item in proposed['evidence']:
        item['attribution']['client_count'] = count
    assert validate_profile_updates([proposed], ctx())[0]['status'] == 'insufficient_evidence'


@pytest.mark.asyncio
async def test_long_unlabelled_dialogue_combines_roles_only_after_all_parts(setup, monkeypatch):
    from agentbridge.sales.service import SalesService
    store, provider = setup
    with store.connect() as db:
        db.execute('UPDATE events SET processed=1')
    first = 'Какие критерии вам важны?\n' + REPLIES[0]
    last = 'Я могу прислать расчёт по оборудованию. Что сравнить?\n' + REPLIES[1]
    text = first + '\n' + 'Несущественный фон разговора.\n' * 650 + last
    ingest(store, 2, text=text)
    # The last seller question is in the preceding part, its client reply in the next.
    boundary = text.index(REPLIES[1])
    monkeypatch.setattr('agentbridge.sales.provider.split_transcript',
        lambda body, size=16000: iter([(0, body[:boundary]), (boundary, body[boundary:])]))
    calls = []
    def run(thread, context, schema=None, **kwargs):
        calls.append(context)
        update = attributed('client@2', 2)
        for item, fragment in zip(update['evidence'], (first, last)):
            item['attribution']['context_quote'] = fragment
        if 'document_part' in context:
            update['status'] = 'tentative'
            update['evidence'] = [item for item in update['evidence']
                if item['role_quote'] in context['events'][0]['text']]
        else:
            summaries = context['events'][0]['document_analysis']
            assert len([p for s in summaries for p in s['communication_observations']]) == 2
        return dict(answer='Рабочая гипотеза C по клиентским критериям.', facts=[],
            basis=[2], next_action='', goal='', client_draft=False,
            communication_profiles=[update] if update['evidence'] else [])
    monkeypatch.setattr(provider, '_run', run)
    await SalesService(store, provider).process(-100, 10)
    saved = store.context(-100, 10)['communication_profiles'][0]
    assert saved['status'] == 'supported' and saved['contact_id'] == 'client@2'
    assert len(store.profile_history(-100, 10, 'client@2')) == 1
    assert sum('document_part' not in c for c in calls) == 1
