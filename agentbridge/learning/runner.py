"""Bounded, restart-safe extraction; rules are never accepted by this worker."""
from __future__ import annotations

import time

MAX_BATCHES = 10
MAX_CHARS = 20000


def validate_candidates(payload, sources):
    if not isinstance(payload, dict) or not isinstance(payload.get('candidates'), list):
        raise ValueError('invalid_learning_response')
    if len(payload['candidates']) > 20:
        raise ValueError('too_many_learning_candidates')
    allowed = {s['source_id']: s for s in sources}
    candidates = []
    for raw in payload['candidates']:
        if not isinstance(raw, dict):
            raise ValueError('invalid_learning_candidate')
        item = dict(raw)
        for key in ('title', 'situation', 'behavior', 'rationale'):
            if not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 1500:
                raise ValueError('invalid_learning_text')
        ids = item.get('source_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in allowed for i in ids):
            raise ValueError('invalid_learning_source')
        keywords = item.get('keywords')
        if not isinstance(keywords, list) or len(keywords) > 30 or any(not isinstance(k, str) or len(k) > 100 for k in keywords):
            raise ValueError('invalid_learning_keywords')
        if item.get('scope') not in {'global', 'chat'}:
            raise ValueError('invalid_learning_scope')
        contexts = {allowed[i]['context'] for i in ids}
        if item['scope'] == 'global':
            item['context'] = ''
        elif len(contexts)!=1 or not next(iter(contexts)):
            raise ValueError('invalid_learning_context')
        else:
            item['context'] = next(iter(contexts))
        if item.get('previous_id') is not None and (type(item['previous_id']) is not int or item['previous_id'] <= 0):
            raise ValueError('invalid_learning_previous')
        candidates.append(item)
    return candidates


def run_learning(store, provider, source_reader, *, now=None):
    cutoff = store.initial_cutoff(time.time() if now is None else now)
    sources = source_reader(cutoff)
    done = store.sources_processed((s['source_id'], s['version']) for s in sources)
    pending = [s for s in sources if (s['source_id'], s['version']) not in done]
    result = dict(batches=0, processed=0, candidates=0, remaining=len(pending))
    seen = {(s['context'], s['text']) for s in sources if (s['source_id'], s['version']) in done}
    while pending and result['batches'] < MAX_BATCHES:
        context = pending[0]['context']
        packet, duplicates, size = [], [], 0
        for source in pending:
            if source['context'] != context:
                continue
            fingerprint = (context, source['text'])
            if fingerprint in seen:
                duplicates.append(source)
                continue
            if size + len(source['text']) > MAX_CHARS:
                break
            packet.append(source)
            size += len(source['text'])
            seen.add(fingerprint)
        if not packet and not duplicates:
            raise ValueError('learning_source_exceeds_packet_limit')
        # Commit after extraction only; provider failure leaves every source pending.
        candidates = validate_candidates(provider.extract(packet), packet) if packet else []
        created = store.commit_batch(packet + duplicates, candidates)
        completed = {(s['source_id'], s['version']) for s in packet + duplicates}
        pending = [s for s in pending if (s['source_id'], s['version']) not in completed]
        result['batches'] += bool(packet)
        result['processed'] += len(completed)
        result['candidates'] += len(created)
        result['remaining'] = len(pending)
    return result
