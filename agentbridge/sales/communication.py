"""Evidence-bound DISC hypotheses; no classifier or extra model turn."""
from __future__ import annotations

from copy import deepcopy
import re


def _object(properties):
    return dict(type='object', additionalProperties=False,
                required=list(properties), properties=properties)


_STRING = {'type': 'string'}
_NULL_STRING = {'type': ['string', 'null']}
_CONFIDENCE = {'type': 'string', 'enum': ['low', 'medium', 'high']}
_STYLE = {'type': ['string', 'null'], 'enum': ['D', 'I', 'S', 'C', None]}
PROFILE_SCHEMA = _object({
    'contact_id': _STRING,
    'status': {'type': 'string', 'enum': ['supported', 'tentative', 'insufficient_evidence']},
    'primary_style': _STYLE, 'secondary_style': _STYLE,
    'role_separation_confidence': _CONFIDENCE, 'profile_confidence': _CONFIDENCE,
    'evidence': {'type': 'array', 'items': _object({
        'event_id': {'type': 'integer'}, 'role_quote': _STRING,
        'quote': _STRING, 'observation': _STRING,
    })},
    'insufficient_reason': _NULL_STRING,
    'recommended_communication': _object({
        'tone': _NULL_STRING, 'structure': _NULL_STRING,
        'emphasize': {'type': 'array', 'items': _STRING},
        'avoid': {'type': 'array', 'items': _STRING},
    }),
    'correction': _object({'event_id': {'type': 'integer'}, 'quote': _STRING,
                           'invalidated_event_ids': {'type': 'array', 'items': {'type': 'integer'}}}),
})
PROFILE_UPDATES_SCHEMA = {'type': 'array', 'items': PROFILE_SCHEMA}

DISC_INSTRUCTIONS = """
communication_profiles — только новые/изменённые DISC-гипотезы по контактам этой сделки,
не полный переписанный снимок; [] если новых оснований/исправления нет. Не нужен отдельный вызов.
DISC не диагноз. Сначала отдели клиентские реплики от менеджера; затем устойчивые признаки:
D — результат, конкретный выбор и краткость; I — диалог, возможности, образ результата;
S — спокойствие, последовательность, без давления; C — факты, критерии, документы, риски.
Не классифицируй по паузе, цене, одному возражению или короткому ответу. Не угадывай роли.
Каждый контакт отдельно. contact_id — точное имя явно подписанного клиента, не выдуманный ID.
Evidence только из явных строк «Клиент Иван: …», «Клиент [Иван]: …» или «Иван (клиент): …».
Общие «Клиент: …», Speaker 1/2, неразделённое аудио, подписи менеджера, пересказ,
CRM/задания, свои тексты не основания для профиля конкретного лица. При неизвестном лице
contact_id='', status=insufficient_evidence, стили null, confidence low, evidence=[].
В evidence до 5 наблюдений: event_id из events/history этой сделки; role_quote — точная
полная клиентская строка с именем и ролью; quote — точная содержательная цитата только
из клиентской части этой строки; observation — краткий признак без скрытых мотивов.
supported: >=2 разных согласующихся содержательных наблюдения, обе confidence >=medium.
tentative: >=1 содержательное наблюдение, роли >=medium. Слабые основания insufficient.
insufficient_evidence: стили null, profile_confidence low, evidence=[], причина заполнена,
recommended_communication tone/structure null, emphasize/avoid пустые.
При гипотезе primary_style обязателен, secondary_style другой или null, причина null.
Не выводи численные проценты. Профиль меняет тон/структуру/детальность, не факты,
цель, аргумент, условия, обещания и следующий шаг. Прямые пожелания клиента приоритетнее.
tentative применяй осторожно, insufficient — нейтрально; DISC не выводи в каждом ответе.
Если менеджер исправил атрибуцию или вывод: correction.event_id текущего события,
correction.quote точная цитата исправления, invalidated_event_ids старые источники,
которые больше нельзя использовать. Не используй их повторно; без оставшихся оснований
сохрани insufficient_evidence. Обычная correction={event_id:0,quote:'',invalidated_event_ids:[]}.
Новое наблюдение может опровергать прежний профиль; объясняй гипотезу, не фиксируй навсегда.
"""


def insufficient(profile, reason):
    profile.update(status='insufficient_evidence', primary_style=None, secondary_style=None,
                   profile_confidence='low', evidence=[], insufficient_reason=reason,
                   recommended_communication=dict(tone=None, structure=None, emphasize=[], avoid=[]))
    return profile


def _client_line(line, contact):
    # Parse explicit attribution only; semantics/DISC stay in the main LLM analysis.
    patterns = (rf'Клиент\s+\[{re.escape(contact)}\]\s*:\s*(.+)',
                rf'Клиент\s+{re.escape(contact)}\s*:\s*(.+)',
                rf'{re.escape(contact)}\s*\(клиент\)\s*:\s*(.+)')
    return next((match.group(1) for pattern in patterns
                 if (match := re.fullmatch(pattern, line.strip(), flags=re.I))), None)


def _bounded_text(value, limit=1000, nullable=False):
    return (value is None and nullable) or (isinstance(value, str) and 0 < len(value.strip()) <= limit)


def validate_profile_updates(value, context):
    """Raise on malformed contracts; neutralize ungrounded hypotheses before persistence."""
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError('invalid communication profiles')
    events = {row['id']: row for row in context.get('history', []) + context.get('events', [])}
    current = {row['id']: row for row in context.get('events', [])}
    existing = {row['contact_id']: row for row in context.get('communication_profiles', [])}
    contacts, result = set(), []
    for original in value:
        if not isinstance(original, dict) or set(original) != set(PROFILE_SCHEMA['properties']):
            raise ValueError('invalid communication profile fields')
        profile = deepcopy(original)
        contact = profile['contact_id']
        if not isinstance(contact, str) or len(contact) > 120 or contact != contact.strip() or '\n' in contact or contact in contacts:
            raise ValueError('invalid or repeated profile contact')
        contacts.add(contact)
        if profile['status'] not in ('supported', 'tentative', 'insufficient_evidence'):
            raise ValueError('invalid profile status')
        for field in ('primary_style', 'secondary_style'):
            if profile[field] not in ('D', 'I', 'S', 'C', None):
                raise ValueError('invalid profile style')
        for field in ('role_separation_confidence', 'profile_confidence'):
            if profile[field] not in ('low', 'medium', 'high'):
                raise ValueError('invalid profile confidence')
        if profile['primary_style'] is not None and profile['primary_style'] == profile['secondary_style']:
            raise ValueError('profile styles must differ')
        guidance = profile['recommended_communication']
        if not isinstance(guidance, dict) or set(guidance) != {'tone', 'structure', 'emphasize', 'avoid'}:
            raise ValueError('invalid profile guidance')
        if any(not _bounded_text(guidance[field], nullable=True) for field in ('tone', 'structure')):
            raise ValueError('invalid profile guidance text')
        if any(not isinstance(guidance[field], list) or len(guidance[field]) > 4 or
               any(not _bounded_text(item, 300) for item in guidance[field]) for field in ('emphasize', 'avoid')):
            raise ValueError('invalid profile guidance list')
        correction = profile['correction']
        if not isinstance(correction, dict) or set(correction) != {'event_id', 'quote', 'invalidated_event_ids'}:
            raise ValueError('invalid profile correction')
        invalidated = correction['invalidated_event_ids']
        if type(correction['event_id']) is not int or not isinstance(invalidated, list) or len(invalidated) > 20 or any(type(i) is not int for i in invalidated):
            raise ValueError('invalid correction sources')
        if correction['event_id']:
            source = current.get(correction['event_id'])
            old_ids = {item['event_id'] for item in existing.get(contact, {}).get('evidence', [])}
            if not source or source.get('source') not in ('manager_feedback', 'telegram_text') or not _bounded_text(correction['quote']) or correction['quote'] not in source['text'] or not set(invalidated) <= old_ids:
                raise ValueError('ungrounded profile correction')
            if not re.search(r'исправ|ошиб|неверн|не\s+клиент|менеджер|не\s+соглас|не\s+подтверж', correction['quote'], re.I):
                raise ValueError('profile correction must be explicit')
        elif correction['quote'] != '' or invalidated:
            raise ValueError('correction requires current source')
        evidence = profile['evidence']
        if not isinstance(evidence, list) or len(evidence) > 5:
            raise ValueError('invalid profile evidence')
        valid, seen = [], set()
        invalidated_ids = set(invalidated) | set(existing.get(contact, {}).get('invalidated_event_ids', []))
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {'event_id', 'role_quote', 'quote', 'observation'} or type(item['event_id']) is not int or any(not _bounded_text(item[field]) for field in ('role_quote', 'quote', 'observation')):
                raise ValueError('invalid profile evidence item')
            source = events.get(item['event_id'])
            role_line = item['role_quote']
            body = _client_line(role_line, contact) if contact and '\n' not in role_line else None
            # Exclude audio manager captions, even when they contain fabricated speaker labels.
            text = source['text'] if source else ''
            if source and source.get('source') == 'audio_transcript':
                # Offset comes from durable audio metadata, never from a caption delimiter.
                offset = source.get('speech_offset')
                text = text[offset:] if type(offset) is int and offset >= 0 else ''
            if source and source.get('source') not in ('telegram_text', 'manager_feedback', 'text_document', 'audio_transcript'):
                text = ''
            key = ' '.join(item['quote'].casefold().split())
            substantive = len(item['quote'].split()) >= 5 and len(item['quote']) >= 25
            if source and item['event_id'] not in invalidated_ids and role_line in text.splitlines() and body and item['quote'] in body and substantive and key not in seen:
                seen.add(key)
                valid.append(item)
        profile['evidence'] = valid
        status = profile['status']
        if status == 'insufficient_evidence':
            reason = profile['insufficient_reason']
            if not _bounded_text(reason):
                raise ValueError('insufficient profile requires reason')
            insufficient(profile, reason)
        elif not contact or not valid or profile['role_separation_confidence'] == 'low' or not profile['primary_style'] or not guidance['tone'] or not guidance['structure']:
            insufficient(profile, 'Недостаточно явно атрибутированных содержательных реплик клиента.')
        elif status == 'supported' and (len(valid) < 2 or profile['profile_confidence'] == 'low'):
            profile['status'] = 'tentative'
            profile['insufficient_reason'] = None
        else:
            profile['insufficient_reason'] = None
        # Missing/omitted fresh evidence never erases a durable prior hypothesis.
        if contact in existing and not correction['event_id'] and not any(
                item['event_id'] in current for item in valid):
            continue
        result.append(profile)
    return result
