from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import sqlite3

import pytest

from agentbridge.agents.codex import CodexProvider, is_usage_limit_error, limit_reset_hint
from agentbridge.application import AgentBridgeApplication
from agentbridge.storage.sqlite import (
    CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN,
    CODEX_RECOVERED_NOTICE,
    _CODEX_LIMIT_KEY,
    _CODEX_LIMIT_REASON_KEY,
    ChatThreadStore,
    codex_limit_notice,
    codex_limit_retry,
    codex_limit_reset_local,
    codex_reset_moment_utc,
)
from agentbridge.logging import redact_secrets
from agentbridge.telegram.bot import due_report_windows
from agentbridge.telegram.polling import PollingHeartbeat
from scripts import diagnose as health
from tests.test_application import QueryProvider

LIMIT_ERROR = SimpleNamespace(message="You've hit your usage limit. try again at 11:27 AM.")


def _note_limit(store: ChatThreadStore, reset_hint: str = "11:27", *, local: str | None = None) -> None:
    """Записать лимит так же, как это делает composition root в main.py."""
    store.note_codex_usage_limit(
        reset_hint, source_timezone_name="Europe/Moscow", local_hint=local if local is not None else f"{reset_hint} Moscow (UTC+03:00, исходное «{reset_hint}» в зоне Europe/Moscow)",
    )


def _limit_deliveries(store: ChatThreadStore) -> tuple[list[str], list[str]]:
    with sqlite3.connect(store.database_path) as conn:
        limit = [row[0] for row in conn.execute("SELECT text FROM owner_query_deliveries WHERE text LIKE '%Лимит Codex исчерпан%'")]
        recovered = [row[0] for row in conn.execute("SELECT text FROM owner_query_deliveries WHERE text LIKE '%снова отвечает%'")]
    return limit, recovered


def _future_reset_hint(hours: int = 2) -> str:
    """Часы сброса из будущего в зоне Codex — иначе момент уже прошёл."""
    session = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow"))
    return (session + timedelta(hours=hours)).strftime("%H:%M")


def test_daily_report_moscow_boundary_and_catchup() -> None:
    before = datetime(2026, 9, 27, 4, 29, tzinfo=timezone.utc)
    after = datetime(2026, 9, 27, 4, 30, tzinfo=timezone.utc)
    assert due_report_windows(before, "2026-09-25", "07:30", "Europe/Moscow") == []
    windows = due_report_windows(after, "2026-09-24", "07:30", "Europe/Moscow")
    assert [item[0] for item in windows] == ["2026-09-25", "2026-09-26"]
    assert windows[-1][1] == "2026-09-25T21:00:00+00:00"
    assert windows[-1][2] == "2026-09-26T21:00:00+00:00"


def test_daily_report_exactly_once_and_zero_learning(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    report_id = store.queue_daily_report("2026-09-26", "2026-09-25T21:00:00+00:00", "2026-09-26T21:00:00+00:00")
    assert store.queue_daily_report("2026-09-26", "2026-09-25T21:00:00+00:00", "2026-09-26T21:00:00+00:00") == report_id
    with sqlite3.connect(store.database_path) as conn:
        text = conn.execute("SELECT text FROM owner_query_deliveries WHERE id=?", (report_id,)).fetchone()[0]
        assert "ничего не узнал" in text
        assert conn.execute("SELECT count(*) FROM daily_reports").fetchone()[0] == 1


def test_crash_notice_queued_once_after_unclean_run(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    assert store.start_run() is None
    previous = store.start_run()
    assert previous
    first = store.queue_operational_notice(f"crash:{previous}", "Recovered")
    assert store.queue_operational_notice(f"crash:{previous}", "Recovered") == first
    store.stop_run()
    assert store.start_run() is None


def test_daily_report_counts_durable_learning_and_errors(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    timestamp = "2026-09-26T12:00:00+00:00"
    with sqlite3.connect(store.database_path) as conn:
        conn.execute("INSERT INTO memory_entries (content,scope,author_user_id,author_name,source_draft_id,status,kind,created_at) VALUES ('fact','global',1,'owner',1,'active','fact',?)", (timestamp,))
        conn.execute("INSERT INTO learning_rules (chat_name,rule_text,scope,author_user_id,author_name,source_draft_id,status,created_at) VALUES ('chat','rule','global',1,'owner',1,'active',?)", (timestamp,))
        conn.execute("INSERT INTO experience_entries (chat_name,situation,lesson,status,created_at) VALUES ('chat','s','l','active',?)", (timestamp,))
        conn.execute("INSERT INTO operational_events(event,level,created_at) VALUES ('failure','ERROR',?)", (timestamp,))
    report_id = store.queue_daily_report("2026-09-26", "2026-09-25T21:00:00+00:00", "2026-09-26T21:00:00+00:00")
    with sqlite3.connect(store.database_path) as conn:
        text = conn.execute("SELECT text FROM owner_query_deliveries WHERE id=?", (report_id,)).fetchone()[0]
    assert "Новых записей памяти: 1; правил: 1; опыта: 1" in text
    assert "ошибок 1" in text


def test_daily_report_counts_every_active_memory_kind(tmp_path: Path) -> None:
    """Отчёт считает всю подтверждённую память, а не только kind='fact'.

    Подтверждённое решение или договорённость для владельца так же ценны,
    как факт, поэтому «Новых фактов» показывало бы меньше, чем Рик выучил."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    timestamp = "2026-09-26T12:00:00+00:00"
    with sqlite3.connect(store.database_path) as conn:
        for index, kind in enumerate(("fact", "decision", "commitment", "preference", "open_question", "rule", "assumption", "experience")):
            conn.execute(
                "INSERT INTO memory_entries (content,scope,author_user_id,author_name,source_draft_id,status,kind,created_at)"
                " VALUES (?,'global',1,'owner',?,'active',?,?)",
                (f"entry-{index}", index + 1, kind, timestamp),
            )
        # Неактивные записи памяти — не то, что Рик выучил: их не считаем.
        for index, status in enumerate(("rejected", "superseded")):
            conn.execute(
                "INSERT INTO memory_entries (content,scope,author_user_id,author_name,source_draft_id,status,kind,created_at)"
                " VALUES (?,'global',1,'owner',?,?,'fact',?)",
                (f"inactive-{status}", 100 + index, status, timestamp),
            )
    report_id = store.queue_daily_report("2026-09-26", "2026-09-25T21:00:00+00:00", "2026-09-26T21:00:00+00:00")
    with sqlite3.connect(store.database_path) as conn:
        text = conn.execute("SELECT text FROM owner_query_deliveries WHERE id=?", (report_id,)).fetchone()[0]
    assert "Новых записей памяти: 8" in text
    assert "Новых фактов" not in text
    assert "ничего не узнал" not in text


def test_daily_report_ignores_inactive_memory(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    with sqlite3.connect(store.database_path) as conn:
        conn.execute("INSERT INTO memory_entries (content,scope,author_user_id,author_name,source_draft_id,status,kind,created_at) VALUES ('x','global',1,'owner',1,'rejected','fact','2026-09-26T12:00:00+00:00')")
        conn.execute("INSERT INTO memory_drafts (recommendation_id,author_user_id,author_name,content,scope,project_key,kind,global_allowed,status,created_at,updated_at) VALUES (NULL,1,'owner','y','global',NULL,'fact',1,'pending','2026-09-26T12:00:00+00:00','2026-09-26T12:00:00+00:00')")
    report_id = store.queue_daily_report("2026-09-26", "2026-09-25T21:00:00+00:00", "2026-09-26T21:00:00+00:00")
    with sqlite3.connect(store.database_path) as conn:
        text = conn.execute("SELECT text FROM owner_query_deliveries WHERE id=?", (report_id,)).fetchone()[0]
    assert "Новых записей памяти: 0" in text
    assert "ничего не узнал" in text


def test_diagnose_opens_database_read_only(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "runtime" / "agentbridge.sqlite3"
    ChatThreadStore(db)
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(health, "RUNTIME", db.parent)
    monkeypatch.setattr(health, "DB", db)
    monkeypatch.setattr(health, "LOGS", db.parent / "logs")
    output = health.diagnose()
    assert "sqlite.health=ok" in output
    assert "process_status_unknown" in output
    # Метки лимита показываются всегда, чтобы по diagnose было видно и
    # отсутствие лимита, и устаревшую метку.
    assert "codex.limit_active=false" in output
    assert "codex.limit_recovery_probe" not in output
    assert "codex.verdict=ok" in output


def test_diagnose_reports_limit_lifecycle_and_probe_verdict(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "runtime" / "agentbridge.sqlite3"
    store = ChatThreadStore(db)
    hint = _future_reset_hint()
    _note_limit(store, hint)
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice(hint))
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(health, "RUNTIME", db.parent)
    monkeypatch.setattr(health, "DB", db)
    monkeypatch.setattr(health, "LOGS", db.parent / "logs")
    output = health.diagnose()
    assert "codex.limit_active=true" in output
    assert "codex.limit_seen_at=2" in output  # метка времени лимита записана
    assert f"codex.limit_reset_hint={hint}" in output
    assert "codex.limit_recovery_probe=WAIT (before_reset_time" in output
    # Метка активна, а свежих падений в логах нет — вердикт про устаревшую метку.
    assert "codex.verdict=stale_limit_flag" in output


def test_diagnose_separates_real_codex_failure_from_stale_flag(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "runtime" / "agentbridge.sqlite3"
    ChatThreadStore(db)
    logs = tmp_path / "runtime" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "agentbridge.log").write_text(
        "timestamp=2026-09-27T07:00:00.000+00:00 level=INFO component=codex event=codex_turn_finished\n"
        "timestamp=2026-09-27T07:30:00.000+00:00 level=ERROR component=codex event=codex_turn_failed reason=boom\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(health, "RUNTIME", db.parent)
    monkeypatch.setattr(health, "DB", db)
    monkeypatch.setattr(health, "LOGS", logs)
    output = health.diagnose()
    # Падение после последнего успеха — это настоящая поломка, а не метка.
    assert "codex.active_failures=1" in output
    assert "codex.verdict=real_failure" in output
    assert "codex.last_success=2026-09-27T07:00:00.000+00:00" in output
    assert "codex.last_failure=2026-09-27T07:30:00.000+00:00" in output


def test_only_successful_poll_updates_durable_heartbeat(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    heartbeat = PollingHeartbeat(on_success=store.record_poll_success)
    heartbeat.mark_finished(False)
    with sqlite3.connect(store.database_path) as conn:
        assert conn.execute("SELECT value FROM operational_state WHERE key='poll_success_at'").fetchone() is None
    heartbeat.mark_finished(True)
    with sqlite3.connect(store.database_path) as conn:
        assert conn.execute("SELECT value FROM operational_state WHERE key='poll_success_at'").fetchone()


def test_openai_credentials_are_redacted() -> None:
    sample = "sk-proj-ABCDEFGHIJKLMNOPQRST Bearer abcdefghijklmnopqrstuvwxyz"
    redacted = redact_secrets(sample)
    assert "ABCDEFGHIJKLMNOPQRST" not in redacted
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted


def test_only_explicit_usage_limit_is_treated_as_limit() -> None:
    limit = SimpleNamespace(message="You've hit your usage limit. try again at 11:27 AM.")
    assert is_usage_limit_error(limit) is True
    assert is_usage_limit_error(SimpleNamespace(message="codex_error_info: usage_limit_exceeded")) is True
    # Соседние ошибки не должны выдавать уведомление о лимите.
    for message in ("429 Too Many Requests", "request timed out after 30s",
                    "The 'gpt-6-luna' model is not supported", "Unauthorized 401"):
        assert is_usage_limit_error(SimpleNamespace(message=message)) is False


def test_limit_reset_time_handles_midnight_and_noon() -> None:
    def hint(text: str) -> str:
        return limit_reset_hint(SimpleNamespace(message=f"usage_limit_exceeded, {text}"))

    # Время выходит без зонной подписи: Codex печатает его в зоне сессии.
    assert hint("try again at 11:27 AM") == "11:27"
    assert hint("try again at 3:07 PM") == "15:07"
    assert hint("try again at 12:05 AM") == "00:05"
    assert hint("try again at 12:30 PM") == "12:30"
    # Неправильное время из текста ошибки не должно превращаться в вывод.
    assert hint("try again at 13:99 AM") == ""
    assert hint("try again at 0:30 AM") == ""


def test_limit_notice_mentions_reset_time_and_omits_it_when_unknown() -> None:
    assert "11:27 UTC" in codex_limit_notice("11:27 UTC")
    assert "восстановление в" not in codex_limit_notice("")


def test_usage_limit_notifies_once_and_recovery_once() -> None:
    notices: list[tuple[str, bool]] = []
    recoveries: list[bool] = []
    provider = CodexProvider(
        on_usage_limit=lambda hint, notify=True: notices.append((hint, notify)),
        on_usage_recovered=lambda: recoveries.append(True),
    )
    provider._note_failure(LIMIT_ERROR)
    provider._note_failure(LIMIT_ERROR)
    provider._note_failure(LIMIT_ERROR)
    provider._note_success()
    provider._note_success()
    provider._note_failure(LIMIT_ERROR)
    provider._note_success()
    assert provider._usage_exhausted is False
    # Владельцу сообщают только о первом лимите; повторы лишь обновляют метку.
    assert [notify for _, notify in notices] == [True, False, False, True]
    assert len(recoveries) == 2


def test_durable_limit_notice_pair_survives_restart(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    first = store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("11:27 UTC"))
    # Повторные отказы не плодят сообщения владельцу.
    assert store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("11:27 UTC")) == first
    assert store.codex_usage_limit_active() is True
    # Метка живёт в SQLite, поэтому новый процесс не пришлёт лимит повторно.
    reopened = ChatThreadStore(tmp_path / "state.sqlite3")
    assert reopened.codex_usage_limit_active() is True
    reopened.clear_operational_state(_CODEX_LIMIT_KEY)
    assert reopened.codex_usage_limit_active() is False
    second = reopened.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("11:27 UTC"))
    assert second != first
    recovered = reopened.queue_operational_notice("codex_usage_limit:recovered", CODEX_RECOVERED_NOTICE)
    with sqlite3.connect(reopened.database_path) as conn:
        texts = [row[0] for row in conn.execute(
            "SELECT text FROM owner_query_deliveries WHERE id IN (?,?,?)", (first, second, recovered),
        )]
    assert sum("Лимит Codex исчерпан" in item for item in texts) == 2
    assert sum("снова отвечает" in item for item in texts) == 1


def test_limit_restart_probe_and_recovery_clears_persistent_state(tmp_path: Path) -> None:
    """Полный сценарий: лимит → рестарт → успешный turn → одно уведомление.

    Это и есть production-баг без фикса: после рестарта новый провайдер не
    знал о прошлом лимите, метка в SQLite оставалась навсегда, а уведомление
    о восстановлении не приходило."""
    notices: list[bool] = []
    recoveries: list[bool] = []
    store = ChatThreadStore(tmp_path / "state.sqlite3")

    def on_limit(hint: str, notify: bool = True) -> None:
        notices.append(notify)
        _note_limit(store, hint)
        if notify:
            store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice(f"{hint} Moscow (UTC+03:00)"))

    def on_recovered() -> None:
        store.clear_codex_usage_limit()
        recoveries.append(True)
        store.queue_operational_notice("codex_usage_limit:recovered", CODEX_RECOVERED_NOTICE)

    def first_process() -> CodexProvider:
        return CodexProvider(
            usage_limit_active=store.codex_usage_limit_active(),
            on_usage_limit=on_limit,
            on_usage_recovered=on_recovered,
        )

    before_restart = first_process()
    before_restart._note_failure(LIMIT_ERROR)
    assert store.codex_usage_limit_active() is True
    assert notices == [True]

    # Рестарт: SQLite — источник истины, поэтому новый провайдер стартует
    # «с лимитом», а не с чистого состояния.
    after_restart = first_process()
    assert after_restart.usage_limit_active is True
    # Первая успешная попытка после восстановления снимает метку и присылает
    # ровно одно уведомление о восстановлении.
    after_restart._note_success()
    assert after_restart.usage_limit_active is False
    assert store.codex_usage_limit_active() is False
    assert store.codex_usage_limit_reason() == ""
    assert store.codex_usage_limit_probe_state().reset_at_utc is None
    assert recoveries == [True]
    limit_texts, recovered_texts = _limit_deliveries(store)
    assert len(limit_texts) == 1
    assert len(recovered_texts) == 1


def test_failed_recovery_probe_rearms_cooldown_without_new_notice(tmp_path: Path) -> None:
    """Контрольная попытка может снова упереться в лимит — и это не тишина.

    Отказ заново ставит паузу (иначе Рик долбил бы модель на каждый запрос),
    но владельцу второе уведомление о том же лимите не уходит."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    notified: list[bool] = []

    def on_limit(hint: str, notify: bool = True) -> None:
        notified.append(notify)
        _note_limit(store, hint)
        if notify:
            store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice(hint))

    def on_recovered() -> None:
        store.clear_codex_usage_limit()
        store.queue_operational_notice("codex_usage_limit:recovered", CODEX_RECOVERED_NOTICE)

    provider = CodexProvider(
        usage_limit_active=store.codex_usage_limit_active(),
        on_usage_limit=on_limit,
        on_usage_recovered=on_recovered,
    )
    provider._note_failure(LIMIT_ERROR)
    assert notified == [True]
    # Пауза после первого отказа.
    first_seen = store.codex_usage_limit_probe_state().seen_at
    assert store.codex_usage_limit_retry(now=first_seen + timedelta(minutes=5)).allowed is False
    # Контрольная попытка разрешена, но лимит ещё не восстановился.
    assert store.codex_usage_limit_retry(now=first_seen + CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN).allowed is True
    provider._note_failure(LIMIT_ERROR)
    assert notified == [True, False]
    # Пауза снова на месте от нового отказа, а уведомление не продублировано.
    second_seen = store.codex_usage_limit_probe_state().seen_at
    assert second_seen > first_seen
    assert store.codex_usage_limit_retry(now=second_seen + timedelta(minutes=5)).allowed is False
    limit_texts, _ = _limit_deliveries(store)
    assert len(limit_texts) == 1


def test_repeated_failures_after_restart_do_not_duplicate_notice(tmp_path: Path) -> None:
    """Рестарт при всё ещё активном лимите не должен слать второе уведомление."""
    sent: list[bool] = []
    store = ChatThreadStore(tmp_path / "state.sqlite3")

    def on_limit(hint: str, notify: bool = True) -> None:
        _note_limit(store, hint)
        if notify:
            sent.append(store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice(hint)) > 0)

    def build() -> CodexProvider:
        return CodexProvider(
            usage_limit_active=store.codex_usage_limit_active(),
            on_usage_limit=on_limit,
            on_usage_recovered=store.clear_codex_usage_limit,
        )

    build()._note_failure(LIMIT_ERROR)
    for _ in range(3):
        # Каждый отказ — как в новом процессе: провайдер сразу «exhausted».
        build()._note_failure(LIMIT_ERROR)
    assert sent == [True]
    limit_texts, _ = _limit_deliveries(store)
    assert len(limit_texts) == 1
    assert store.codex_usage_limit_active() is True


def test_other_failures_do_not_touch_the_limit_notice() -> None:
    notified: list[bool] = []
    provider = CodexProvider(on_usage_limit=lambda hint, notify=True: notified.append(notify))
    provider._note_failure(RuntimeError("connection reset by peer"))
    assert provider._usage_exhausted is False
    assert notified == []


def test_provider_restored_as_exhausted_keeps_owner_silent() -> None:
    """Провайдер, собранный с сохранённой меткой, молчит про лимит.

    Иначе после каждого рестарта владелец получал бы «Лимит Codex исчерпан»
    заново, хотя про это уже сказано в прошлом процессе."""
    notified: list[bool] = []
    provider = CodexProvider(usage_limit_active=True, on_usage_limit=lambda hint, notify=True: notified.append(notify))
    assert provider.usage_limit_active is True
    provider._note_failure(LIMIT_ERROR)
    assert notified == [False]
    # Без стартовой метки тот же отказ, наоборот, один раз сообщает владельцу.
    fresh = CodexProvider(on_usage_limit=lambda hint, notify=True: notified.append(notify))
    fresh._note_failure(LIMIT_ERROR)
    assert notified == [False, True]


def test_known_reset_time_prevents_premature_retry(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    _note_limit(store, _future_reset_hint())
    state = store.codex_usage_limit_probe_state()
    assert state.active is False  # метка уведомления ещё не ставилась
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("x"))
    state = store.codex_usage_limit_probe_state()
    assert state.active is True
    assert state.reset_at_utc is not None
    # До названного сброса попытка не делается: она заведомо отказная.
    before = state.reset_at_utc - timedelta(minutes=1)
    retry = store.codex_usage_limit_retry(now=before)
    assert retry.allowed is False
    assert retry.reason == "before_reset_time"
    assert retry.wait_seconds == 60


def test_after_reset_time_next_request_probes_codex(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    _note_limit(store, _future_reset_hint())
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("x"))
    state = store.codex_usage_limit_probe_state()
    after = state.reset_at_utc + timedelta(minutes=1)
    retry = store.codex_usage_limit_retry(now=after)
    assert retry.allowed is True
    assert retry.reason == "reset_time_reached"


def test_unknown_reset_time_uses_bounded_cooldown(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    _note_limit(store, "")
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice(""))
    state = store.codex_usage_limit_probe_state()
    assert state.reset_at_utc is None
    # Сразу после отказа ждём: долбёжка модели тут не нужна.
    just_after = state.seen_at + timedelta(seconds=10)
    retry = store.codex_usage_limit_retry(now=just_after)
    assert retry.allowed is False
    assert retry.reason == "cooldown"
    assert retry.wait_seconds == int(CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN.total_seconds()) - 10
    # Через паузу — ровно одна контрольная попытка.
    later = state.seen_at + CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN + timedelta(seconds=1)
    assert store.codex_usage_limit_retry(now=later).allowed is True
    # Повторный отказ заново ставит паузу от себя: частота ограничена сверху,
    # поэтому лимит не проверяется снова и снова на каждый запрос.
    _note_limit(store, "")
    refreshed = store.codex_usage_limit_probe_state().seen_at
    assert refreshed > state.seen_at
    assert store.codex_usage_limit_retry(now=refreshed + timedelta(minutes=6)).allowed is False
    assert store.codex_usage_limit_retry(now=refreshed + CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN + timedelta(seconds=1)).allowed is True


def test_retry_state_survives_restart(tmp_path: Path) -> None:
    """Пауза живёт в SQLite: рестарт не должен обнулять таймер и долбить модель."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    _note_limit(store, "")
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice(""))
    reopened = ChatThreadStore(tmp_path / "state.sqlite3")
    seen_at = reopened.codex_usage_limit_probe_state().seen_at
    assert seen_at is not None
    assert reopened.codex_usage_limit_retry(now=seen_at + timedelta(seconds=30)).allowed is False


def test_cooldown_and_reset_decision_need_no_model_call() -> None:
    """Решение о попытке принимается по часам, без фонового опроса Codex."""
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    reset_at = now + timedelta(minutes=10)
    assert codex_limit_retry(reset_at, now, now=now).allowed is False
    assert codex_limit_retry(reset_at, now, now=now + timedelta(minutes=11)).reason == "reset_time_reached"
    assert codex_limit_retry(None, None, now=now).allowed is True
    assert codex_limit_retry(None, now, now=now).reason == "cooldown"
    assert codex_limit_retry(None, now, now=now + CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN).allowed is True


def test_reset_moment_uses_session_zone_and_rejects_past_time() -> None:
    now = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
    # 11:27 по Москве — это 08:27 UTC в тот же день.
    assert codex_reset_moment_utc("11:27", "Europe/Moscow", now=now) == datetime(2026, 9, 27, 8, 27, tzinfo=timezone.utc)
    # 03:00 по Москве уже прошло — ждать до утра бессмысленно.
    assert codex_reset_moment_utc("03:00", "Europe/Moscow", now=now) is None
    assert codex_reset_moment_utc("11:27", "Not/AZone", now=now) is None
    assert codex_reset_moment_utc("", "Europe/Moscow", now=now) is None
    assert codex_reset_moment_utc("мусор", "Europe/Moscow", now=now) is None


def test_reset_time_is_translated_from_session_zone() -> None:
    # В тексте ошибки Codex зоны нет, поэтому пересчёт опирается на зону
    # сессии и честно подписывает и исходные часы, и предполагаемую зону.
    def local(zone: str, source: str = "Europe/Moscow") -> str:
        return codex_limit_reset_local("11:27", zone, session_timezone_name=source)

    assert local("Asia/Novosibirsk") == (
        "15:27 Novosibirsk (UTC+07:00, исходное «11:27» в зоне Europe/Moscow)"
    )
    assert local("Europe/Moscow") == "11:27 Moscow (UTC+03:00, исходное «11:27» в зоне Europe/Moscow)"
    assert local("Asia/Yekaterinburg") == (
        "13:27 Yekaterinburg (UTC+05:00, исходное «11:27» в зоне Europe/Moscow)"
    )
    # Если зона сессии Codex поменяется, пересчёт последует за ней.
    assert "исходное «11:27» в зоне UTC" in local("Asia/Novosibirsk", "UTC")
    # Переход через полночь: 23:59 по Москве — это уже 03:59 следующих суток.
    assert codex_limit_reset_local("23:59", "Asia/Novosibirsk", session_timezone_name="Europe/Moscow").startswith("03:59 Novosibirsk")
    assert codex_limit_reset_local("00:30", "Asia/Novosibirsk", session_timezone_name="Europe/Moscow").startswith("04:30 Novosibirsk")
    # Негодные данные не ломают уведомление, а остаются как есть.
    assert codex_limit_reset_local("", "Asia/Novosibirsk", session_timezone_name="Europe/Moscow") == ""
    assert codex_limit_reset_local("мусор", "Asia/Novosibirsk", session_timezone_name="Europe/Moscow") == "мусор"
    # Неизвестная зона не превращается в выдуманное время: подпись прямо говорит,
    # что зона не указана.
    assert "не указал зону" in codex_limit_reset_local("11:27", "Asia/Novosibirsk", session_timezone_name="Not/AZone")
    # Нерабочая зона владельца тоже не даёт «None» в тексте уведомления.
    assert "None" not in codex_limit_reset_local("11:27", "Not/AZone", session_timezone_name="Europe/Moscow")


def test_saved_reset_hint_is_readable_and_cleared(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    # Время сброса берём заведомо из будущего, иначе момент уже прошёл и
    # ждать до него смысла нет.
    future = (datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow")) + timedelta(hours=2)).strftime("%H:%M")
    _note_limit(store, future, local=f"{future} UTC")
    assert store.codex_usage_limit_reason() == f"{future} UTC"
    assert store.codex_usage_limit_probe_state().reset_at_utc is not None
    # Повторный отказ обновляет время, а не теряет его.
    later = (datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow")) + timedelta(hours=3)).strftime("%H:%M")
    _note_limit(store, later, local=f"{later} UTC")
    assert store.codex_usage_limit_reason() == f"{later} UTC"
    # Восстановление снимает всё состояние лимита разом, включая момент сброса.
    store.clear_codex_usage_limit()
    assert store.codex_usage_limit_reason() == ""
    state = store.codex_usage_limit_probe_state()
    assert (state.active, state.reset_at_utc, state.seen_at) == (False, None, None)


@pytest.mark.asyncio
async def test_limit_replaces_clarification_placeholder(tmp_path: Path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = QueryProvider()
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=7654321)
    # Без лимита Рик уточняет охват и до Codex не доходит.
    plain = await service.handle_owner_query("Ну что там?")
    assert "Уточните" in plain.text
    assert "Лимит Codex" not in plain.text
    attempts_without_limit = provider.owner_threads_created

    # Лимит со сбросом далеко в будущем: ответ внятный, к Codex не идём.
    _note_limit(store, "23:59", local="23:59 Novosibirsk (UTC+07:00)")
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("23:59 Novosibirsk (UTC+07:00)"))
    limited = await service.handle_owner_query("Ну что там?")
    assert "Лимит Codex исчерпан" in limited.text
    assert "23:59" in limited.text
    assert "Ну что там?" in limited.text
    assert "Уточните" not in limited.text
    # Известный лимит не должен стоить ещё одной попытки обращения к Codex.
    assert provider.owner_threads_created == attempts_without_limit


@pytest.mark.asyncio
async def test_owner_query_probes_codex_after_reset_time(tmp_path: Path, chat_registry) -> None:
    """Лимит не блокирует Рика навсегда: после сброса запрос идёт в Codex.

    Без этого был бы тупик: лимит восстановился, а Рик вечно отвечает
    заглушкой, ни разу не проверив модель."""
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = QueryProvider()
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=7654321)
    _note_limit(store, _future_reset_hint())
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("x"))
    assert service._codex_limit_answer("вопрос") is not None
    # Момент сброса переносим в прошлое — как будто лимит уже восстановился.
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(store.database_path) as conn:
        conn.execute(
            "UPDATE operational_state SET value=? WHERE key='codex_usage_limit:reset_at_utc'", (expired,),
        )
    assert service._codex_limit_answer("вопрос") is None
    answer = await service.handle_owner_query("Ну что там по Acme?", reply_to_message_id=None)
    # Запрос дошёл до обычного разбора и модели, а не превратился в заглушку.
    assert "Лимит Codex исчерпан" not in answer.text
    assert provider.owner_threads_created == 1


@pytest.mark.asyncio
async def test_owner_query_does_not_probe_codex_before_reset_time(tmp_path: Path, chat_registry) -> None:
    """До названного сброса контрольной попытки нет: запрос заведомо отказный."""
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = QueryProvider()
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=7654321)
    future = (datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow")) + timedelta(hours=2)).strftime("%H:%M")
    _note_limit(store, future)
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice(future))
    assert service._codex_limit_answer("вопрос") is not None
    limited = await service.handle_owner_query("Ну что там?")
    assert "Лимит Codex исчерпан" in limited.text
    assert provider.owner_threads_created == 0
