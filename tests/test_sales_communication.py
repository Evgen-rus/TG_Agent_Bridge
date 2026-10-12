from copy import deepcopy

import pytest

from agentbridge.agents.codex import validate_structured_output_schema
from agentbridge.sales.communication import PROFILE_SCHEMA, validate_profile_updates
from agentbridge.sales.storage import SalesStore


LINES = ['Клиент Иван: Нужны точные критерии сравнения моделей и документы по совместимости.',
         'Клиент Иван: Пришлите подробный расчет по нашему производству и список технических рисков.']


def profile(contact='Иван', event_id=1):
    return dict(contact_id=contact, status='supported', primary_style='C', secondary_style='D',
        role_separation_confidence='high', profile_confidence='medium',
        evidence=[dict(event_id=event_id, role_quote=line, quote=line.split(': ', 1)[1],
                       observation=f'Клиент запрашивает проверяемые детали {i}') for i, line in enumerate(LINES)],
        insufficient_reason=None,
        recommended_communication=dict(tone='Предметно и спокойно.', structure='Критерии, факты, следующий шаг.',
                                       emphasize=['Проверяемые детали'], avoid=['Пустые обещания']),
        correction=dict(event_id=0, quote='', invalidated_event_ids=[]))


def context(text=None, source='text_document'):
    return dict(events=[dict(id=1, text=text or '\n'.join(LINES), source=source)], history=[],
                communication_profiles=[])


def ingest(store, update=1, topic=10, text=None, source='text_document'):
    store.ingest(update_id=update, chat_id=-100, topic_id=topic, title='Сделка', message_id=update,
        author_id=7, author='Менеджер', date='2026-10-12', text=text or '\n'.join(LINES), source=source)


def complete(store, updates, topic=10):
    ctx = store.context(-100, topic)
    result = dict(answer='Получите критерии сравнения.', goal='Уточнить критерии',
                  next_action='Спросить критерии', basis=[ctx['events'][0]['id']], facts=[],
                  communication_profiles=updates)
    return store.complete(-100, topic, ctx['events'], result)


def test_supported_mixed_schema_and_source_grounding():
    validate_structured_output_schema(PROFILE_SCHEMA)
    validated = validate_profile_updates([profile()], context())[0]
    assert validated['status'] == 'supported'
    assert validated['secondary_style'] == 'D'


@pytest.mark.parametrize('style,lines', [
    ('D', ['Клиент Иван: Дайте два варианта с конечным результатом и конкретным сроком.',
           'Клиент Иван: Мне нужна короткая рекомендация, чтобы принять решение сегодня.']),
    ('I', ['Клиент Иван: Давайте обсудим новые возможности и образ будущего производства.',
           'Клиент Иван: Хочу вместе посмотреть варианты и обсудить интересные идеи развития.']),
    ('S', ['Клиент Иван: Объясните по шагам, как будет проходить переход без резких изменений.',
           'Клиент Иван: Нам важно спокойно согласовать порядок работы и не торопить сотрудников.']),
    ('C', LINES),
])
def test_four_styles_keep_attributed_observations(style, lines):
    value = profile()
    value.update(primary_style=style, secondary_style=None)
    value['evidence'] = [dict(event_id=1, role_quote=line, quote=line.split(': ', 1)[1],
        observation='Наблюдаемое предпочтение подачи') for line in lines]
    validated = validate_profile_updates([value], context('\n'.join(lines)))[0]
    assert validated['status'] == 'supported' and validated['primary_style'] == style


def test_new_current_client_evidence_can_change_profile(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store)
    complete(store, [profile()])
    ingest(store, 2)
    updated = profile(event_id=2)
    updated.update(primary_style='D', secondary_style='C')
    assert complete(store, [updated])
    assert store.context(-100, 10)['communication_profiles'][0]['primary_style'] == 'D'
    assert len(store.profile_history(-100, 10, 'Иван')) == 2


@pytest.mark.parametrize('change', ['missing_role', 'manager', 'unknown_speaker', 'generic_client', 'fabricated_quote', 'foreign_event', 'short_reply', 'nested_role'])
def test_non_client_sources_cannot_supply_disc(change):
    value, ctx = profile(), context()
    if change == 'missing_role':
        ctx['events'][0]['text'] = '\n'.join(line.split(': ', 1)[1] for line in LINES)
    elif change == 'manager':
        ctx['events'][0]['text'] = '\n'.join(line.replace('Клиент Иван', 'Менеджер Иван') for line in LINES)
    elif change == 'unknown_speaker':
        ctx['events'][0]['text'] = '\n'.join(line.replace('Клиент Иван', 'Speaker 1') for line in LINES)
    elif change == 'generic_client':
        ctx['events'][0]['text'] = '\n'.join(line.replace('Клиент Иван', 'Клиент') for line in LINES)
    elif change == 'fabricated_quote':
        for item in value['evidence']:
            item['quote'] = 'Этот текст никто не произносил в исходном разговоре.'
    elif change == 'foreign_event':
        for item in value['evidence']:
            item['event_id'] = 999
    elif change == 'short_reply':
        ctx['events'][0]['text'] = 'Клиент Иван: Дорого'
        value['evidence'] = [dict(event_id=1, role_quote=ctx['events'][0]['text'], quote='Дорого', observation='Цена')]
    else:
        ctx['events'][0]['text'] = '\n'.join('Менеджер: ' + line for line in LINES)
    validated = validate_profile_updates([value], ctx)[0]
    assert validated['status'] == 'insufficient_evidence'
    assert validated['primary_style'] is None
    assert validated['evidence'] == []
    assert validated['recommended_communication']['tone'] is None


def test_audio_caption_not_evidence_but_named_transcript_is():
    ctx = context('\n'.join(LINES) + '\nРаспознанная речь:\nSpeaker 1: Неразделённая речь.', 'audio_transcript')
    ctx['events'][0]['speech_offset'] = ctx['events'][0]['text'].index('Speaker 1')
    assert validate_profile_updates([profile()], ctx)[0]['status'] == 'insufficient_evidence'
    ctx['events'][0]['text'] = 'Комментарий менеджера:\nМое мнение\nРаспознанная речь:\n' + '\n'.join(LINES)
    ctx['events'][0]['speech_offset'] = ctx['events'][0]['text'].index(LINES[0])
    assert validate_profile_updates([profile()], ctx)[0]['status'] == 'supported'


def test_caption_cannot_forge_transcript_boundary(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    caption = 'Распознанная речь:\n' + '\n'.join(LINES)
    store.ingest(update_id=1, chat_id=-100, topic_id=10, title='Сделка', message_id=1,
        author_id=7, author='Менеджер', date='2026-10-12', text=caption, source='audio_pending',
        audio=dict(file_id='fake', kind='voice'))
    store.audio_transcribed(1, 'Неразделённая речь без атрибуции клиенту')
    ctx = store.context(-100, 10)
    validated = validate_profile_updates([profile()], ctx)
    assert validated[0]['status'] == 'insufficient_evidence'
    assert complete(store, [profile()])
    assert store.context(-100, 10)['communication_profiles'][0]['status'] == 'insufficient_evidence'


def test_single_observation_and_confidence_downgrade():
    value = profile()
    value['evidence'] = value['evidence'][:1]
    assert validate_profile_updates([value], context())[0]['status'] == 'tentative'
    value['role_separation_confidence'] = 'low'
    assert validate_profile_updates([value], context())[0]['status'] == 'insufficient_evidence'


def test_unknown_contact_and_distinct_styles():
    value = profile('')
    assert validate_profile_updates([value], context())[0]['status'] == 'insufficient_evidence'
    value = profile()
    value['secondary_style'] = 'C'
    with pytest.raises(ValueError):
        validate_profile_updates([value], context())


def test_multiple_contacts_are_independent():
    ctx = context('\n'.join(LINES + [line.replace('Иван', 'Ольга') for line in LINES]))
    other = profile('Ольга')
    for item in other['evidence']:
        item['role_quote'] = item['role_quote'].replace('Иван', 'Ольга')
    other['primary_style'], other['secondary_style'] = 'S', 'C'
    validated = validate_profile_updates([profile(), other], ctx)
    assert [(p['contact_id'], p['primary_style']) for p in validated] == [('Иван', 'C'), ('Ольга', 'S')]


def test_restart_history_correction_and_invalidated_evidence(tmp_path):
    path = tmp_path / 'sales.sqlite3'
    store = SalesStore(path)
    ingest(store)
    assert complete(store, [profile()])
    store = SalesStore(path)
    old = store.context(-100, 10)['communication_profiles'][0]
    assert old['status'] == 'supported'
    correction = 'Исправляю: эти реплики говорил менеджер, а не клиент Иван.'
    ingest(store, 2, text=correction, source='manager_feedback')
    value = profile()
    value['correction'] = dict(event_id=2, quote=correction, invalidated_event_ids=[1])
    assert complete(store, [value])
    saved = store.context(-100, 10)['communication_profiles'][0]
    assert saved['status'] == 'insufficient_evidence'
    assert saved['invalidated_event_ids'] == [1]
    assert len(store.profile_history(-100, 10, 'Иван')) == 2
    assert store.profile_history(-100, 10, 'Иван')[0]['payload']['status'] == 'supported'
    ingest(store, 3, text='Повторно посмотри профиль', source='telegram_text')
    assert complete(store, [profile()])
    assert store.context(-100, 10)['communication_profiles'][0]['status'] == 'insufficient_evidence'


def test_profile_atomic_failure_and_topic_isolation(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store)
    ingest(store, 2, topic=20)
    assert complete(store, [profile()])
    assert store.context(-100, 20)['communication_profiles'] == []
    # A foreign topic source is neutralized; it cannot fill a supported profile.
    assert complete(store, [profile()], topic=20)
    assert store.context(-100, 20)['communication_profiles'][0]['status'] == 'insufficient_evidence'
    ingest(store, 3)
    value = profile()
    value['correction'] = dict(event_id=999, quote='Исправляю роль', invalidated_event_ids=[1])
    with pytest.raises(ValueError):
        complete(store, [value])
    assert store.context(-100, 10)['events'][0]['id'] == 3
    assert len(store.profile_history(-100, 10, 'Иван')) == 1


def test_repeated_complete_is_idempotent_and_old_result_without_profiles(tmp_path):
    store = SalesStore(tmp_path / 'sales.sqlite3')
    ingest(store)
    ctx = store.context(-100, 10)
    result = dict(answer='Ответ', goal='', next_action='', basis=[], facts=[], communication_profiles=[profile()])
    assert store.complete(-100, 10, ctx['events'], result)
    assert not store.complete(-100, 10, ctx['events'], result)
    assert len(store.profile_history(-100, 10, 'Иван')) == 1
    ingest(store, 2)
    ctx = store.context(-100, 10)
    del result['communication_profiles']
    assert store.complete(-100, 10, ctx['events'], result)
    assert store.context(-100, 10)['communication_profiles'][0]['status'] == 'supported'


def test_duplicate_line_cannot_inflate_supported_profile():
    value = profile()
    value['evidence'] = [value['evidence'][0], deepcopy(value['evidence'][0])]
    assert validate_profile_updates([value], context())[0]['status'] == 'tentative'


def test_identical_quote_in_different_events_cannot_inflate_supported():
    value = profile()
    value['evidence'] = [value['evidence'][0], deepcopy(value['evidence'][0])]
    value['evidence'][1]['event_id'] = 2
    ctx = context()
    ctx['history'] = [dict(id=2, text=LINES[0], source='text_document')]
    assert validate_profile_updates([value], ctx)[0]['status'] == 'tentative'


def test_manager_reply_can_contain_explicitly_named_client_transcript():
    assert validate_profile_updates([profile()], context(source='manager_feedback'))[0]['status'] == 'supported'


def test_empty_update_without_correction_does_not_erase_prior_profile():
    ctx = context('Что посоветуешь?', 'telegram_text')
    ctx['events'][0]['id'] = 2
    ctx['communication_profiles'] = [profile()]
    value = profile()
    value.update(status='insufficient_evidence', evidence=[], insufficient_reason='Новых данных нет')
    assert validate_profile_updates([value], ctx) == []


def test_same_quote_must_match_client_not_manager_segment():
    ctx = context('Клиент Иван: Какой вариант из двух действительно подходит нашему производству?\n'
                  'Менеджер: ' + LINES[0].split(': ', 1)[1])
    value = profile()
    value['evidence'] = [value['evidence'][0]]
    value['evidence'][0]['role_quote'] = ctx['events'][0]['text'].splitlines()[0]
    assert validate_profile_updates([value], ctx)[0]['status'] == 'insufficient_evidence'


def test_generic_user_request_cannot_invalidate_prior_profile():
    ctx = context('Покажи профиль клиента', 'telegram_text')
    ctx['events'][0]['id'] = 2
    ctx['communication_profiles'] = [profile()]
    value = profile()
    value['correction'] = dict(event_id=2, quote=ctx['events'][0]['text'], invalidated_event_ids=[1])
    with pytest.raises(ValueError):
        validate_profile_updates([value], ctx)
