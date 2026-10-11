from __future__ import annotations

import asyncio
import logging
import re
from typing import Protocol

logger = logging.getLogger(__name__)
FIELDS = ('client', 'subject', 'stage', 'client_goal', 'needs', 'decision_makers',
          'confirmed', 'obstacles', 'manager_promises', 'client_promises', 'next_step',
          'deadlines', 'unknowns', 'risks', 'last_interaction')
KINDS = ('fact', 'assumption', 'unknown', 'agreement', 'manager_action', 'client_response')


class SalesProvider(Protocol):
    async def analyze(self, context: dict) -> dict: ...


def validate_result(result, context):
    if not isinstance(result, dict) or set(result) != {'answer', 'facts', 'next_action', 'goal', 'basis'}:
        raise ValueError('Invalid sales result')
    for key in ('answer', 'next_action', 'goal'):
        if not isinstance(result[key], str) or len(result[key]) > (8000 if key == 'answer' else 1000):
            raise ValueError('Invalid response text')
    if not result['answer'].strip():
        raise ValueError('Empty answer')
    sources = {r['id']: r['text'] for r in context['history'] + context['events']}
    current_ids = {row['id'] for row in context['events']}
    existing = {row['id']: row for row in context['facts']}
    available_sources = set(sources) | {row['event_id'] for row in context['facts']}
    if not isinstance(result['basis'], list) or len(result['basis']) > 20 or any(
        type(i) is not int or i not in available_sources for i in result['basis']
    ):
        raise ValueError('Invalid recommendation source')
    if result['next_action'].strip() and not result['basis']:
        raise ValueError('Action without source')
    if not isinstance(result['facts'], list) or len(result['facts']) > 25:
        raise ValueError('Invalid card patch')
    replaced = set()
    clean = []
    for fact in result['facts']:
        if not isinstance(fact, dict) or set(fact) != {'field', 'text', 'kind', 'event_id', 'quote', 'replaces'}:
            raise ValueError('Invalid fact')
        if fact['field'] not in FIELDS or fact['kind'] not in KINDS:
            raise ValueError('Invalid classification')
        if any(not isinstance(fact[key], str) or not fact[key].strip() or len(fact[key]) > 1000 for key in ('text', 'quote')):
            raise ValueError('Invalid fact text')
        if type(fact['event_id']) is not int or fact['event_id'] not in current_ids or fact['quote'] not in sources[fact['event_id']]:
            raise ValueError('Fact has no current source')
        if fact['field'] == 'unknowns' and fact['kind'] != 'unknown':
            raise ValueError('Unknown cannot be confirmed')
        if fact['kind'] in {'fact', 'agreement'} and re.search(
            r'(?i)не зна[ею]|неизвестн|неясн|нет данных|не определ[её]н|не подтвержд[её]н', fact['quote']
        ):
            raise ValueError('Explicit uncertainty cannot become a fact')
        old_id = fact['replaces']
        if type(old_id) is not int or old_id < 0:
            raise ValueError('Invalid replacement')
        if old_id:
            old = existing.get(old_id)
            if old is None or old_id in replaced or old['field'] != fact['field']:
                raise ValueError('Cross-deal or repeated replacement')
            # Facts are immutable unless the manager explicitly corrects or changes them.
            correction = re.search(r'(?i)исправ|уточн|ошиб|неверн|вместо|теперь|отмен|передум|измен|не .+ а ', sources[fact['event_id']])
            if old['kind'] not in {'unknown', 'assumption'} and not correction:
                raise ValueError('Unsupported fact replacement')
            if old['kind'] not in {'unknown', 'assumption'} and fact['kind'] in {'unknown', 'assumption'}:
                raise ValueError('Unsupported downgrade')
            replaced.add(old_id)
        elif any(all(old[key] == fact[key] for key in ('field', 'text', 'kind')) for old in existing.values()):
            continue
        clean.append(fact)
    return {**result, 'facts': clean}


class SalesService:
    def __init__(self, store, provider: SalesProvider):
        self.store, self.provider = store, provider
        self._lock = asyncio.Lock()

    async def process(self, chat_id, topic_id):
        async with self._lock:
            context = await asyncio.to_thread(self.store.context, chat_id, topic_id)
            if not context['events']:
                return
            try:
                result = validate_result(await self.provider.analyze(context), context)
                await asyncio.to_thread(self.store.complete, chat_id, topic_id, context['events'], result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log prompts, model output or transport exception contents.
                logger.warning('event=sales_analysis_failed error_type=%s', type(exc).__name__)
                await asyncio.to_thread(self.store.failed, chat_id, topic_id, context['events'][0]['id'])
