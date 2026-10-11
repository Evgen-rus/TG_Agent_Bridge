"""Owner-only technical audit. No message bodies, prompts or session files."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import re

from .agents.base import AgentProvider
from .logging import redact_secrets
from .storage.sqlite import ChatThreadStore

logger = logging.getLogger(__name__)

# Only literal event names produced by Bridge; never arbitrary log fields.
AUDIT_EVENTS = frozenset("""
codex_turn_started codex_turn_finished codex_turn_failed codex_tool_completed codex_tool_recovered codex_request_repeated
codex_thread_replaced codex_thread_reset codex_transport_closed_retry
codex_transport_closed_retry_failed codex_usage_limit_exhausted codex_usage_limit_recovered
owner_memory_agent_failed owner_memory_compaction_retry owner_memory_rejected_size
owner_memory_git_commit_failed owner_memory_updated owner_memory_unchanged
owner_memory_update_started owner_memory_thread_reset owner_query_thread_reset sepia_thread_reset
delivery_attempt delivery_success owner_delivery_pending owner_query_delivery_pending
telegram_send_rejected telegram_transient_error reminder_delivery_pending reminder_sent
previous_run_unclean process_started process_stopped self_restart_abandoned
telegram_poll_stalled telegram_poll_restart_started telegram_poll_restart_success
telegram_poll_restart_failed telegram_polling_fatal
client_batch_failed batch_started batch_finished catchup_started catchup_finished
general_task_failed developer_task_plan_failed general_task_plan_failed
owner_query_scope_resolution_failed owner_portfolio_aggregate_failed
sepia_refactor_failed recommendation_revised media_download_retry media_download_failed
self_audit_failed self_audit_delivery_pending codex_tool_telemetry_unavailable
""".split())
TOOL_TYPES = frozenset({'commandExecution', 'mcpToolCall', 'dynamicToolCall', 'fileChange', 'webSearch'})
FAILURE_KINDS = frozenset({'codex_sandbox_missing', 'codex_transport_closed', 'codex_usage_limit', 'codex_auth', 'codex_turn_failed'})
_FIELD = re.compile(r'\b([a-z_]+)=([^\s]+)')


def collect_summary(store: ChatThreadStore, log_dir: Path, end_utc: str) -> dict:
    end = datetime.fromisoformat(end_utc)
    start, baseline = end - timedelta(hours=24), end - timedelta(days=8)
    evidence: dict[str, dict] = {}

    def add(source: str, event: str, timestamp: datetime, *, ref: str, failure_kind: str = '') -> None:
        if event not in AUDIT_EVENTS or not baseline <= timestamp < end:
            return
        key = f'{source}:{event}'
        item = evidence.setdefault(key, {'source': source, 'event': event, 'current_count': 0,
                                         'previous_7d_count': 0, 'days': {}, 'first_utc': timestamp.isoformat(),
                                         'last_utc': timestamp.isoformat(), 'sample_refs': [], 'failure_kinds': {}})
        item['current_count' if timestamp >= start else 'previous_7d_count'] += 1
        day = timestamp.date().isoformat()
        item['days'][day] = item['days'].get(day, 0) + 1
        item['first_utc'] = min(item['first_utc'], timestamp.isoformat())
        item['last_utc'] = max(item['last_utc'], timestamp.isoformat())
        if timestamp >= start:
            if len(item['sample_refs']) < 3:
                item['sample_refs'].append(ref)
            if failure_kind in FAILURE_KINDS:
                item['failure_kinds'][failure_kind] = item['failure_kinds'].get(failure_kind, 0) + 1

    logs = {'files_read': 0, 'files_unreadable': 0, 'first_utc': None, 'last_utc': None}
    failures: dict[str, datetime] = {}
    recoveries = Counter()
    requests = set()
    tools = defaultdict(Counter)
    # Correlation identifiers stay local; only aggregate counts leave this function.
    for path in sorted(log_dir.glob('agentbridge.log*'), key=lambda path: (path.name == 'agentbridge.log', path.name)):
        if not re.fullmatch(r'agentbridge\.log(?:\.\d{4}-\d{2}-\d{2})?', path.name) or not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open(encoding='utf-8', errors='replace') as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.startswith('timestamp=') or len(line) > 16000:
                        continue
                    fields = dict(_FIELD.findall(line))
                    try:
                        timestamp = datetime.fromisoformat(fields['timestamp'])
                        if timestamp.tzinfo is None or not baseline <= timestamp < end:
                            continue
                    except (KeyError, ValueError):
                        continue
                    event = fields.get('event', '')
                    add('logs', event, timestamp, ref=f'{path.name}:{line_number}', failure_kind=fields.get('failure_kind', ''))
                    request_key = fields.get('request_key', '')
                    if event == 'codex_turn_started' and re.fullmatch(r'[0-9a-f]{64}', request_key):
                        if request_key in requests:
                            add('logs', 'codex_request_repeated', timestamp, ref=f'{path.name}:{line_number}')
                        requests.add(request_key)
                    stamp = timestamp.isoformat()
                    logs['first_utc'] = min(logs['first_utc'] or stamp, stamp)
                    logs['last_utc'] = max(logs['last_utc'] or stamp, stamp)
                    thread = fields.get('thread_id', '')
                    if event == 'codex_turn_failed' and thread:
                        failures[thread] = timestamp
                    if event == 'codex_turn_finished' and thread in failures:
                        failed_at = failures.pop(thread)
                        if timestamp >= start and timestamp >= failed_at:
                            recoveries['failed_turn_then_success_same_thread'] += 1
                    if event == 'codex_tool_completed' and fields.get('tool_type') in TOOL_TYPES:
                        status = fields.get('status')
                        if status in {'completed', 'failed', 'declined', 'unknown'}:
                            period = 'current' if timestamp >= start else 'previous_7d'
                            tools[f"{period}:{fields['tool_type']}"][status] += 1
            logs['files_read'] += 1
        except OSError:
            logs['files_unreadable'] += 1

    with store._connect() as connection:
        for row in connection.execute("""SELECT id, event, created_at FROM operational_events
                WHERE created_at>=? AND created_at<?""", (baseline.isoformat(), end.isoformat())):
            add('sqlite.operational_events', row['event'], datetime.fromisoformat(row['created_at']), ref=f"operational_events.id={row['id']}")
        pending = {
            'inbox': connection.execute("SELECT count(*) FROM telegram_messages WHERE processing_status IN ('pending','processing')").fetchone()[0],
            'owner_deliveries': connection.execute("SELECT count(*) FROM owner_query_deliveries WHERE owner_message_id IS NULL").fetchone()[0],
        }
        decisions = [dict(row) for row in connection.execute(
            "SELECT issue, status, evidence_json FROM self_audit_proposals WHERE id IN (SELECT max(id) FROM self_audit_proposals GROUP BY issue)")]
    return {
        'window': {'start_utc': start.isoformat(), 'end_utc': end.isoformat(), 'baseline_start_utc': baseline.isoformat()},
        'evidence': evidence, 'log_coverage': logs, 'tool_counts': dict(tools),
        'recovery_indicators': dict(recoveries), 'pending_now': pending,
        'previous_decisions': [{'issue': row['issue'], 'status': row['status'],
                                'current_count': json.loads(row['evidence_json'])['current_count'],
                                'failure_kinds': json.loads(row['evidence_json']).get('failure_kinds', {})} for row in decisions],
        'limitations': [
            'Log and SQLite counts overlap; never add them together.',
            'Logs may cover less than eight days; missing days are unavailable, not zero.',
            'Pending counts are a current snapshot, not historical window counts.',
            'Tool metadata starts at deployment; only completed SDK items are observed.',
            'No commands, arguments, outputs, prompts or messages are extracted or forwarded; Codex session files are not read.',
            'Exact textual request repeats use process-local HMACs; fingerprints change after restart; media inputs are unavailable.',
            'Repeated turns and later successes are indicators; usefulness and causal correction are unproven.',
        ],
    }


def validate_proposals(payload: dict, summary: dict) -> list[dict]:
    items = payload.get('proposals') if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) > 3:
        raise ValueError('Invalid audit result')
    proposals, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Invalid audit proposal')
        issue = item.get('issue')
        evidence = summary['evidence'].get(issue)
        if evidence is None or not evidence['current_count'] or issue in seen:
            raise ValueError('Audit evidence missing or repeated')
        seen.add(issue)
        for field in ('proposal', 'cause', 'benefit', 'risk'):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 400 or redact_secrets(value) != value:
                raise ValueError('Invalid audit text')
        if item.get('priority') not in {'P1', 'P2', 'P3'} or not isinstance(item.get('uncertain'), bool):
            raise ValueError('Invalid audit priority/confidence')
        # Canonical event identity prevents a model paraphrase from bypassing rejection.
        canonical = evidence['event']
        if canonical in {proposal['issue'] for proposal in proposals}:
            continue
        text = (f"Самоаудит Рика — {item['priority']}\n{item['proposal']}\n"
                f"Факт: {evidence['event']}; источник {issue} ({', '.join(evidence.get('sample_refs', []))}).\n"
                f"Повторяемость: {evidence['current_count']} за 24 ч; {evidence['previous_7d_count']} за предыдущие 7 дней "
                f"(по доступным данным).\nПериод: {summary['window']['start_utc']} — {summary['window']['end_utc']}.\n"
                f"Вероятная причина ({'неуверенная гипотеза' if item['uncertain'] else 'гипотеза'}): {item['cause']}\n"
                f"Польза: {item['benefit']}\nРиск: {item['risk']}\n"
                "Принятие согласует только идею. Для кода: /dev и отдельное подтверждение задачи.")
        proposals.append({'issue': canonical, 'evidence': evidence, 'text': text})
    return proposals


class SelfAudit:
    def __init__(self, store: ChatThreadStore, provider: AgentProvider, log_dir: Path):
        self.store, self.provider, self.log_dir = store, provider, log_dir
        self.task: asyncio.Task | None = None

    def schedule(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name='rick-self-audit')

    async def _run(self) -> None:
        run = None
        try:
            run = await asyncio.to_thread(self.store.claim_self_audit)
            if run is None:
                return
            if await asyncio.to_thread(self.store.codex_usage_limit_active):
                await asyncio.to_thread(self.store.finish_self_audit, run['report_date'], [], failed=True)
                return
            summary = await asyncio.to_thread(collect_summary, self.store, self.log_dir, run['end_utc'])
            payload = await self.provider.analyze_self_audit(summary=summary)
            proposals = validate_proposals(payload, summary)
            await asyncio.to_thread(self.store.finish_self_audit, run['report_date'], proposals)
            logger.info('event=self_audit_completed component=audit candidates=%d', len(proposals))
        except asyncio.CancelledError:
            # Persisted running work is recovered only by the next process.
            raise
        except Exception:
            # No exception text: it can contain SDK response/prompt fragments.
            logger.error('event=self_audit_failed component=audit')
            if run is not None:
                try:
                    await asyncio.to_thread(self.store.finish_self_audit, run['report_date'], [], failed=True)
                except Exception:
                    logger.error('event=self_audit_state_failed component=audit')

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
