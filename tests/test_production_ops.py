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
    _CODEX_LIMIT_RESET_AT_KEY,
    _CODEX_LIMIT_SEEN_AT_KEY,
    _CODEX_RECOVERED_KEY,
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
# Тот же отказ по лимиту, но без разбираемого момента сброса: storage не может
# посчитать reset_at и держит только общую паузу CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN.
UNKNOWN_RESET_LIMIT_ERROR = SimpleNamespace(message="You've hit your usage limit. try again later.")


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


class LimitBridge:
    """Связка провайдеров с SQLite ровно как в `agentbridge.main`.

    Решение «новый лимит или продолжение» принимает storage по сохранённой
    метке, а не локальный флаг провайдера. Тесты ходят через неё, иначе они
    проверяли бы удобный, но не настоящий путь решения.
    """

    def __init__(self, store: ChatThreadStore, *, session_timezone: str = "Europe/Moscow", owner_timezone: str = "Asia/Novosibirsk") -> None:
        self.store = store
        self.session_timezone = session_timezone
        self.owner_timezone = owner_timezone

    def _on_limit(self, reset_hint: str) -> None:
        local_hint = codex_limit_reset_local(
            reset_hint, self.owner_timezone, session_timezone_name=self.session_timezone,
        )
        # Метки жизненного цикла не трогаем: сброс метки восстановления делает
        # сам storage внутри той же транзакции, как и в agentbridge.main.
        self.store.claim_codex_usage_limit(
            reset_hint, codex_limit_notice(local_hint),
            source_timezone_name=self.session_timezone, local_hint=local_hint,
        )

    def _on_recovered(self, turn_started_at: str | None = None) -> None:
        self.store.claim_codex_usage_recovered(CODEX_RECOVERED_NOTICE, turn_started_at=turn_started_at)

    def provider(self) -> CodexProvider:
        """Новый процесс: стартовое состояние берётся из SQLite."""
        return CodexProvider(
            usage_limit_active=self.store.codex_usage_limit_active(),
            on_usage_limit=self._on_limit,
            on_usage_recovered=self._on_recovered,
        )

    def notices(self) -> tuple[list[str], list[str]]:
        return _limit_deliveries(self.store)


def _after_limit_start(store: ChatThreadStore, seconds: int = 5) -> str:
    """Момент старта turn, заведомо позже последнего отказа по лимиту.

    Такой turn «знал» о лимите, поэтому его успех подтверждает восстановление."""
    seen = store.codex_usage_limit_probe_state().seen_at
    assert seen is not None
    return (seen + timedelta(seconds=seconds)).isoformat()


def _future_seen_at(hours: int = 1) -> str:
    """Метка «когда заметили лимит» из будущего.

    Нужна, чтобы смоделировать turn, который стартовал раньше лимита: время
    отказа уходит вперёд, и наивное сравнение с текущим временем не сработает."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _limit_keys(store: ChatThreadStore) -> set[str]:
    with sqlite3.connect(store.database_path) as conn:
        return {row[0] for row in conn.execute("SELECT key FROM operational_state WHERE key LIKE 'codex_usage_limit%'")}


def _recovery_delivery_ids(store: ChatThreadStore) -> list[int]:
    """ID доставок о восстановлении по порядку: их счёт и есть проверка."""
    with sqlite3.connect(store.database_path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT id FROM owner_query_deliveries WHERE text LIKE '%снова отвечает%' ORDER BY id",
        )]


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
    # Метка активна, но в логах нет ни отказа по лимиту, ни успеха: доказательств
    # нет, поэтому вердикт не вправе назвать метку устаревшей.
    assert "codex.verdict=active_limit_unknown" in output


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


def _write_logs(logs: Path, *lines: str) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "agentbridge.log").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def test_diagnose_does_not_call_a_real_usage_limit_a_stale_flag(tmp_path: Path, monkeypatch) -> None:
    """Активный лимит с отказом по лимиту и без успеха после него — не метка.

    Прежний verdict называл это `likely_stale_limit_flag`, хотя состояние
    полностью штатное: лимит исчерпан, и его проверяет следующий запрос."""
    db = tmp_path / "runtime" / "agentbridge.sqlite3"
    store = ChatThreadStore(db)
    _note_limit(store, _future_reset_hint())
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("11:27"))
    logs = tmp_path / "runtime" / "logs"
    _write_logs(
        logs,
        "timestamp=2026-09-27T07:00:00.000+00:00 level=INFO component=codex event=codex_turn_finished",
        "timestamp=2026-09-27T07:30:00.000+00:00 level=ERROR component=codex event=codex_turn_failed reason=limit",
        "timestamp=2026-09-27T07:30:00.000+00:00 level=WARNING component=codex event=codex_usage_limit_exhausted reset_hint=11:27",
    )
    for name, value in {"ROOT": tmp_path, "RUNTIME": db.parent, "DB": db, "LOGS": logs}.items():
        monkeypatch.setattr(health, name, value)

    output = health.diagnose()
    assert "codex.limit_active=true" in output
    assert "codex.limit_failures_in_logs=1" in output
    assert "codex.verdict=active_usage_limit" in output
    assert "stale" not in output.split("codex.verdict=")[1].splitlines()[0]


def test_diagnose_calls_the_flag_stale_only_after_a_later_success(tmp_path: Path, monkeypatch) -> None:
    """Устаревшей метку можно назвать только доказательством: успех после отказа."""
    db = tmp_path / "runtime" / "agentbridge.sqlite3"
    store = ChatThreadStore(db)
    _note_limit(store, _future_reset_hint())
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("11:27"))
    logs = tmp_path / "runtime" / "logs"
    _write_logs(
        logs,
        "timestamp=2026-09-27T07:00:00.000+00:00 level=WARNING component=codex event=codex_usage_limit_exhausted reset_hint=11:27",
        "timestamp=2026-09-27T07:30:00.000+00:00 level=ERROR component=codex event=codex_turn_failed reason=limit",
        # Успешный turn после отказа — метка лимита не должна была пережить.
        "timestamp=2026-09-27T08:00:00.000+00:00 level=INFO component=codex event=codex_turn_finished",
    )
    for name, value in {"ROOT": tmp_path, "RUNTIME": db.parent, "DB": db, "LOGS": logs}.items():
        monkeypatch.setattr(health, name, value)

    output = health.diagnose()
    assert "codex.verdict=stale_limit_flag" in output


def test_codex_verdict_never_names_stale_without_evidence() -> None:
    """Логика verdict проверяется напрямую: без доказательств — никакой ярлык.

    `active_failures` приходит уже отфильтрованным — это отказы строго после
    последнего успеха, поэтому непустой список означает «успеха после них не
    было» по построению."""
    # Метка активна, свежий отказ по лимиту, успеха после него нет → лимит активен.
    assert health._codex_verdict(True, ["2026-09-27T07:30:00+00:00"], "2026-09-27T07:00:00+00:00", ["2026-09-27T07:30:00+00:00"]).startswith("active_usage_limit")
    # Доказанное восстановление: отказ по лимиту был, успех после него прошёл.
    assert health._codex_verdict(True, [], "2026-09-27T08:00:00+00:00", ["2026-09-27T07:30:00+00:00"]).startswith("stale_limit_flag")
    # Свежий отказ есть, но про лимит ничего не известно → настоящая поломка.
    assert health._codex_verdict(True, ["2026-09-27T07:30:00+00:00"], "2026-09-27T07:00:00+00:00", []).startswith("real_failure")
    # Всё спокойно.
    assert health._codex_verdict(False, [], "2026-09-27T08:00:00+00:00", []) == "ok"
    # Метки лимита нет даже при отказах — это поломка, а не лимит.
    assert health._codex_verdict(False, ["2026-09-27T07:30:00+00:00"], "2026-09-27T07:00:00+00:00", ["2026-09-27T07:30:00+00:00"]).startswith("real_failure")


def test_active_limit_is_never_called_stale_without_a_proven_later_success() -> None:
    """Метка активна, а доказательств в логах нет — вердикт нейтральный.

    Отсутствие улик не доказательство: раньше `limit_active` с пустым списком
    отказов сразу давал `stale_limit_flag`, хотя успеха после лимита в логах
    могло не быть вовсе."""
    # Метка активна, отказов по лимиту в логах нет вовсе.
    assert health._codex_verdict(True, [], "2026-09-27T08:00:00+00:00", []).startswith("active_limit_unknown")
    # Метка активна, отказ по лимиту есть, но успеха в логах нет вообще.
    assert health._codex_verdict(True, [], None, ["2026-09-27T07:30:00+00:00"]).startswith("active_limit_unknown")
    # Метка активна, отказ по лимиту был, а последний успех РАНЬШЕ него —
    # это активный лимит, а не устаревшая метка.
    verdict = health._codex_verdict(True, [], "2026-09-27T07:00:00+00:00", ["2026-09-27T07:30:00+00:00"])
    assert verdict.startswith("active_usage_limit")


def test_diagnose_reports_active_limit_unknown_without_logs(tmp_path: Path, monkeypatch) -> None:
    """Активный лимит без логов не объявляется устаревшей меткой."""
    db = tmp_path / "runtime" / "agentbridge.sqlite3"
    store = ChatThreadStore(db)
    _note_limit(store, _future_reset_hint())
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("11:27"))
    logs = tmp_path / "runtime" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "agentbridge.log").write_text(
        "timestamp=2026-09-27T07:00:00.000+00:00 level=INFO component=application event=process_starting\n",
        encoding="utf-8",
    )
    for name, value in {"ROOT": tmp_path, "RUNTIME": db.parent, "DB": db, "LOGS": logs}.items():
        monkeypatch.setattr(health, name, value)

    output = health.diagnose()
    assert "codex.limit_active=true" in output
    assert "codex.verdict=active_limit_unknown" in output
    assert "stale_limit_flag" not in output


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


def test_usage_limit_reports_facts_and_owner_notifies_once(tmp_path: Path) -> None:
    """Провайдер сообщает факты, а уведомления считает storage."""
    reported: list[str] = []
    recoveries: list[bool] = []
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)

    def build() -> CodexProvider:
        return CodexProvider(
            on_usage_limit=lambda hint: (reported.append(hint), bridge._on_limit(hint)),
            on_usage_recovered=lambda started=None: (recoveries.append(True), bridge._on_recovered(started)),
        )

    provider = build()
    provider._note_failure(LIMIT_ERROR)
    provider._note_failure(LIMIT_ERROR)
    provider._note_failure(LIMIT_ERROR)
    provider._note_success()
    provider._note_success()
    provider._note_failure(LIMIT_ERROR)
    provider._note_success()
    assert provider._usage_exhausted is False
    # Каждый отказ сообщается наружу, но владельцу уходит одно сообщение на лимит.
    assert len(reported) == 4
    limit_texts, recovered_texts = bridge.notices()
    assert len(limit_texts) == 2
    assert len(recovered_texts) == 2
    # Успех сообщается всегда: решение о том, был ли лимит активен, принимает
    # storage по метке, а не провайдер по своей памяти. Два лишних успеха —
    # это два no-op, а не два уведомления.
    assert len(recoveries) == 3


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
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)

    before_restart = bridge.provider()
    before_restart._note_failure(LIMIT_ERROR)
    assert store.codex_usage_limit_active() is True

    # Рестарт: SQLite — источник истины, поэтому новый провайдер стартует
    # «с лимитом», а не с чистого состояния.
    after_restart = bridge.provider()
    assert after_restart.usage_limit_active is True
    # Первая успешная попытка после восстановления снимает метку и присылает
    # ровно одно уведомление о восстановлении.
    after_restart._note_success()
    assert after_restart.usage_limit_active is False
    assert store.codex_usage_limit_active() is False
    assert store.codex_usage_limit_reason() == ""
    assert store.codex_usage_limit_probe_state().reset_at_utc is None
    limit_texts, recovered_texts = bridge.notices()
    assert len(limit_texts) == 1
    assert len(recovered_texts) == 1


def test_failed_recovery_probe_rearms_cooldown_without_new_notice(tmp_path: Path) -> None:
    """Контрольная попытка может снова упереться в лимит — и это не тишина.

    Отказ заново ставит паузу (иначе Рик долбил бы модель на каждый запрос),
    но владельцу второе уведомление о том же лимите не уходит.

    Здесь намеренно отказ БЕЗ разбираемого времени сброса: только тогда решение
    принимает ветка CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN. Со временем сброса в
    тексте ошибки тест зависел бы от часов на машине — утром «11:27» ещё в
    будущем, storage честно выбирает before_reset_time, и проверялась бы
    совсем другая ветка."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    provider = bridge.provider()
    provider._note_failure(UNKNOWN_RESET_LIMIT_ERROR)
    # Время сброса неизвестно: ждём только ограниченную паузу после отказа.
    assert store.codex_usage_limit_probe_state().reset_at_utc is None
    first_seen = store.codex_usage_limit_probe_state().seen_at
    assert store.codex_usage_limit_retry(now=first_seen + timedelta(minutes=5)).allowed is False
    # Контрольная попытка разрешена, но лимит ещё не восстановился.
    assert store.codex_usage_limit_retry(now=first_seen + CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN).allowed is True
    provider._note_failure(UNKNOWN_RESET_LIMIT_ERROR)
    # Пауза снова на месте от нового отказа, а уведомление не продублировано.
    second_seen = store.codex_usage_limit_probe_state().seen_at
    assert second_seen > first_seen
    assert store.codex_usage_limit_retry(now=second_seen + timedelta(minutes=5)).allowed is False
    limit_texts, _ = bridge.notices()
    assert len(limit_texts) == 1


def test_known_future_reset_time_blocks_until_reset_then_allows(tmp_path: Path) -> None:
    """Ветка известного времени сброса: до него ждём, после — пробуем.

    Отдельный тест от паузы: известный момент сброса ограничивает попытки
    строже, чем CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN, и освобождает сам собой."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    provider = bridge.provider()
    reset_hint = _future_reset_hint(hours=2)
    provider._note_failure(
        SimpleNamespace(message=f"You've hit your usage limit. try again at {reset_hint} AM.")
    )

    state = store.codex_usage_limit_probe_state()
    # Момент сброса распознан и лежит в будущем, поэтому общая пауза не нужна.
    assert state.reset_at_utc is not None
    before = store.codex_usage_limit_retry()
    assert before.allowed is False
    assert before.reason == "before_reset_time"
    # Пауза держит и после кулдауна: до сброса модель не трогаем.
    still_blocked = store.codex_usage_limit_retry(now=state.seen_at + CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN)
    assert still_blocked.allowed is False
    assert still_blocked.reason == "before_reset_time"
    # Сам момент сброса открывает обычную попытку.
    after = store.codex_usage_limit_retry(now=datetime.fromisoformat(state.reset_at_utc))
    assert after.allowed is True
    assert after.reason == "reset_time_reached"


def test_repeated_failures_after_restart_do_not_duplicate_notice(tmp_path: Path) -> None:
    """Рестарт при всё ещё активном лимите не должен слать второе уведомление."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    bridge.provider()._note_failure(LIMIT_ERROR)
    for _ in range(3):
        # Каждый отказ — как в новом процессе: провайдер сразу «exhausted».
        bridge.provider()._note_failure(LIMIT_ERROR)
    limit_texts, _ = bridge.notices()
    assert len(limit_texts) == 1
    assert store.codex_usage_limit_active() is True


def test_other_failures_do_not_touch_the_limit_notice() -> None:
    reported: list[str] = []
    provider = CodexProvider(on_usage_limit=reported.append)
    provider._note_failure(RuntimeError("connection reset by peer"))
    assert provider._usage_exhausted is False
    assert reported == []


def test_provider_restored_as_exhausted_still_reports_new_failures() -> None:
    """Провайдер, собранный с сохранённой меткой, обязан сообщать об отказах.

    Раньше он молчал, полагаясь на локальный флаг, и новый лимит мог пройти
    без уведомления. Теперь решение принимает storage, а провайдер всегда
    докладывает факт."""
    reported: list[str] = []
    provider = CodexProvider(usage_limit_active=True, on_usage_limit=reported.append)
    assert provider.usage_limit_active is True
    provider._note_failure(LIMIT_ERROR)
    assert reported == ["11:27"]


def test_two_providers_start_exhausted_and_stay_consistent(tmp_path: Path) -> None:
    """Оба провайдера стартуют «с лимитом» и снимают флаг при своём успехе."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    bridge.provider()._note_failure(LIMIT_ERROR)

    owner = bridge.provider()
    client = bridge.provider()
    assert (owner.usage_limit_active, client.usage_limit_active) == (True, True)

    owner._note_success()
    assert owner.usage_limit_active is False
    assert client.usage_limit_active is True  # локальная память ещё старая
    limit_texts, recovered_texts = bridge.notices()
    assert (len(limit_texts), len(recovered_texts)) == (1, 1)


def test_second_provider_success_does_not_duplicate_recovery_notice(tmp_path: Path) -> None:
    """Поздний успех второго провайдера безопасен: дубликата нет, состояние цело.

    Это и был баг: provider B помнил старый лимит и мог либо прислать второе
    уведомление о восстановлении, либо снести уже созданное новое состояние."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    bridge.provider()._note_failure(LIMIT_ERROR)
    owner = bridge.provider()
    client = bridge.provider()
    owner._note_success()

    # Успех второго провайдера: storage уже без лимита, поэтому это no-op.
    client._note_success()
    assert client.usage_limit_active is False
    assert store.codex_usage_limit_active() is False
    _, recovered_texts = bridge.notices()
    assert len(recovered_texts) == 1


def test_new_limit_after_recovery_is_reported_once_even_by_stale_provider(tmp_path: Path) -> None:
    """Главный edge-case: новый лимит после чужого восстановления.

    Provider B локально помнит старый лимит, но authoritative состояние уже
    снято. Новый отказ обязан быть новым лимитом: состояние создаётся заново,
    владелец получает ровно одно новое уведомление."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    bridge.provider()._note_failure(LIMIT_ERROR)
    owner = bridge.provider()
    client = bridge.provider()

    # Owner-контур восстанавливается первым и снимает persistent state.
    owner._note_success()
    assert store.codex_usage_limit_active() is False
    assert client.usage_limit_active is True  # B об этом ещё не знает

    # Позже B упирается в уже НОВЫЙ usage limit.
    client._note_failure(LIMIT_ERROR)

    # Это новый лимит, а не продолжение: состояние создано и владельцу ушло
    # ровно одно новое сообщение.
    assert store.codex_usage_limit_active() is True
    limit_texts, recovered_texts = bridge.notices()
    assert len(limit_texts) == 2
    assert len(recovered_texts) == 1
    # Cooldown нового лимита считается от нового отказа, а не от старого.
    state = store.codex_usage_limit_probe_state()
    assert state.active is True
    assert state.seen_at is not None


def test_in_flight_success_from_before_the_limit_cannot_clear_it(tmp_path: Path) -> None:
    """Главный race: успех turn, который стартовал ДО лимита, его не подтверждает.

    Turn A ушёл в модель на остатке лимита, turn B упёрся в исчерпание и
    записал новый лимит, после чего A вернулся успешным. A ничего не знает
    про лимит, который возник в его полёте, поэтому подтверждать восстановление
    он не вправе: иначе настоящий лимит был бы стёрт."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    provider = bridge.provider()

    # Момент старта turn A фиксируем ДО появления лимита.
    turn_a_started = datetime.now(timezone.utc).isoformat()
    limit_seen = _future_seen_at()  # будущий момент: A стартовал заведомо раньше

    # Turn B получает отказ по лимиту — он и создаёт текущий лимит.
    with sqlite3.connect(store.database_path) as conn:
        conn.execute("UPDATE operational_state SET value=? WHERE key=?", (limit_seen, _CODEX_LIMIT_SEEN_AT_KEY))
    provider._note_failure(LIMIT_ERROR)
    assert store.codex_usage_limit_active() is True

    # Turn A завершается успехом уже после появления лимита.
    provider._note_success(turn_a_started)

    # Лимит остался: успех относится к другому моменту времени.
    assert store.codex_usage_limit_active() is True
    assert store.codex_usage_limit_reason() != ""
    limit_texts, recovered_texts = bridge.notices()
    assert len(recovered_texts) == 0
    assert len(limit_texts) == 1


def test_success_started_after_the_limit_clears_it_once(tmp_path: Path) -> None:
    """Обычное восстановление: turn, начатый после лимита, его подтверждает.

    Второй шаг предыдущего теста — чтобы убедиться, что причинная проверка
    отсекает только в-flight turn, а не восстановление целиком."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    provider = bridge.provider()
    provider._note_failure(LIMIT_ERROR)
    limit_seen = store.codex_usage_limit_probe_state().seen_at
    assert limit_seen is not None

    # Turn C стартует после того, как лимит был зафиксирован.
    turn_c_started = (limit_seen + timedelta(seconds=5)).isoformat()
    provider._note_success(turn_c_started)

    assert store.codex_usage_limit_active() is False
    assert store.codex_usage_limit_reason() == ""
    assert store.codex_usage_limit_probe_state().reset_at_utc is None
    limit_texts, recovered_texts = bridge.notices()
    assert len(limit_texts) == 1
    assert len(recovered_texts) == 1
    # Повторный успех того же turn не плодит уведомления.
    provider._note_success(turn_c_started)
    _, recovered_texts = bridge.notices()
    assert len(recovered_texts) == 1


def test_in_flight_turn_from_another_provider_cannot_clear_a_new_limit(tmp_path: Path) -> None:
    """Причинная граница общая для обоих провайдеров, а не локальная привилегия.

    Owner-контур начал turn до лимита, клиентский провайдер его зафиксировал.
    Успех owner-контура не должен снимать чужой лимит — иначе один рано
    завершившийся запрос глушил бы сигнал об исчерпании для всех."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    owner = bridge.provider()
    client = bridge.provider()
    turn_started_before = datetime.now(timezone.utc).isoformat()

    client._note_failure(LIMIT_ERROR)
    seen = store.codex_usage_limit_probe_state().seen_at
    assert seen is not None

    owner._note_success(turn_started_before)
    assert store.codex_usage_limit_active() is True
    _, recovered_texts = bridge.notices()
    assert recovered_texts == []

    # Turn, начатый после лимита, восстановление подтверждает.
    owner._note_success((seen + timedelta(seconds=1)).isoformat())
    assert store.codex_usage_limit_active() is False
    _, recovered_texts = bridge.notices()
    assert len(recovered_texts) == 1


def test_recovery_without_a_known_start_time_still_works(tmp_path: Path) -> None:
    """Неизвестный момент старта причинность не ломает, а проверяет её.

    Старые вызовы и ручные проверки не передают момент старта. Запрещать по
    ним восстановление нельзя: лимит завис бы в базе навсегда, а это хуже,
    чем одно лишнее подтверждение."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    provider = bridge.provider()
    provider._note_failure(LIMIT_ERROR)
    assert store.codex_usage_limit_active() is True
    provider._note_success()
    assert store.codex_usage_limit_active() is False
    _, recovered_texts = bridge.notices()
    assert len(recovered_texts) == 1


def test_new_limit_atomically_clears_the_previous_recovery_marker(tmp_path: Path) -> None:
    """Переход в новый лимит атомарен: старая метка восстановления снимается
    в той же транзакции, что и создание нового лимита.

    Если эти шаги были бы разнесены, сбой между ними оставил бы базу в
    состоянии, где следующий лимит уже не смог бы сообщить о своём
    восстановлении. Проверяем итог: метки старого восстановления больше нет
    ровно в момент появления нового лимита."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    bridge.provider()._note_failure(LIMIT_ERROR)
    bridge.provider()._note_success(_after_limit_start(store))
    assert _limit_keys(store) == {_CODEX_RECOVERED_KEY}
    first_delivery = _recovery_delivery_ids(store)

    # Новый лимит: main.py больше не трогает метки, всё делает storage.
    store.claim_codex_usage_limit("11:27", codex_limit_notice("x"), source_timezone_name="Europe/Moscow", local_hint="x")
    assert _limit_keys(store) == {
        _CODEX_LIMIT_KEY, _CODEX_LIMIT_REASON_KEY, _CODEX_LIMIT_RESET_AT_KEY, _CODEX_LIMIT_SEEN_AT_KEY,
    }
    # Метка восстановления снята, поэтому этот лимит сможет прислать своё.
    store.claim_codex_usage_recovered(CODEX_RECOVERED_NOTICE)
    second_delivery = _recovery_delivery_ids(store)
    # Новый лимит смог прислать своё восстановление: ровно одна новая доставка.
    assert len(second_delivery) == len(first_delivery) + 1
    _, recovered_texts = _limit_deliveries(store)
    assert len(recovered_texts) == 2


def test_repeat_failure_in_the_same_limit_does_not_touch_recovery_lifecycle(tmp_path: Path) -> None:
    """Повторный отказ внутри активного лимита метку восстановления не трогает.

    Иначе лимит, который ещё не завершился, преждевременно получил бы новое
    окно для уведомления о восстановлении, и пара уведомлений разъехалась бы."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    bridge.provider()._note_failure(LIMIT_ERROR)
    bridge.provider()._note_success(_after_limit_start(store))
    marker_after_recovery = _limit_keys(store)
    assert _CODEX_RECOVERED_KEY in marker_after_recovery

    # Новый лимит, затем серия отказов внутри него.
    bridge.provider()._note_failure(LIMIT_ERROR)
    assert _CODEX_RECOVERED_KEY not in _limit_keys(store)
    for _ in range(5):
        bridge.provider()._note_failure(LIMIT_ERROR)
    assert _CODEX_RECOVERED_KEY not in _limit_keys(store)
    # Лимит всё тот же: владельцу не пришло ни одного лишнего уведомления.
    assert store.codex_usage_limit_active() is True
    limit_texts, _ = bridge.notices()
    assert len(limit_texts) == 2
    # И восстановление по-прежнему можно прислать ровно один раз.
    bridge.provider()._note_success(_after_limit_start(store))
    _, recovered_texts = bridge.notices()
    assert len(recovered_texts) == 2


def test_recovery_notice_always_pairs_with_the_limit_notice(tmp_path: Path) -> None:
    """Восстановлений ровно столько же, сколько завершившихся лимитов.

    Пара приходит из одного хранилища, поэтому разъехаться не может: каждый
    лимит, который владельцу заявили и который потом сняли, обязан быть
    закрыт ровно одним уведомлением о восстановлении. Лимит, оставшийся
    активным, закрытия ещё не получил — поэтому лимитов на один больше.
    Это и есть контракт, который раньше мог разъехаться между двумя
    провайдерами."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    for _ in range(3):
        bridge.provider()._note_failure(LIMIT_ERROR)
        bridge.provider()._note_success()
    bridge.provider()._note_failure(LIMIT_ERROR)  # лимит, оставшийся активным
    limit_texts, recovered_texts = bridge.notices()
    assert (len(limit_texts), len(recovered_texts)) == (4, 3)
    assert store.codex_usage_limit_active() is True


def test_successful_turn_always_clears_a_durable_limit(tmp_path: Path) -> None:
    """Успешный turn — достоверное свидетельство, что Codex снова отвечает.

    Даже если провайдер локально не считал себя исчерпанным, метка лимита в
    SQLite обязана исчезнуть: иначе после рестарта Рик продолжил бы отвечать
    «лимит исчерпан», хотя модель давно работает."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    bridge = LimitBridge(store)
    bridge.provider()._note_failure(LIMIT_ERROR)
    assert store.codex_usage_limit_active() is True
    # Провайдер, собранный без стартовой метки, локально «чист».
    unaware = CodexProvider(
        usage_limit_active=False,
        on_usage_limit=bridge._on_limit,
        on_usage_recovered=bridge._on_recovered,
    )
    unaware._note_success()
    assert store.codex_usage_limit_active() is False
    assert store.codex_usage_limit_reason() == ""
    assert store.codex_usage_limit_probe_state().reset_at_utc is None
    limit_texts, recovered_texts = bridge.notices()
    assert (len(limit_texts), len(recovered_texts)) == (1, 1)


def test_concurrent_limit_claims_create_single_notice(tmp_path: Path) -> None:
    """Два провайдера в разных потоках не удваивают уведомление.

    Проверка и запись метки идут в одной транзакции, поэтому второй поток
    видит уже созданный лимит и молчит."""
    from concurrent.futures import ThreadPoolExecutor

    store = ChatThreadStore(tmp_path / "state.sqlite3")
    results: list[bool] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store.claim_codex_usage_limit, "11:27", codex_limit_notice("11:27 Moscow"),
                        source_timezone_name="Europe/Moscow", local_hint="11:27 Moscow")
            for _ in range(2)
        ]
        results = [future.result() for future in futures]
    assert sorted(results) == [False, True]
    limit_texts, _ = _limit_deliveries(store)
    assert len(limit_texts) == 1


def test_concurrent_recovery_claims_create_single_notice(tmp_path: Path) -> None:
    """Два провайдера не присылают два уведомления о восстановлении."""
    from concurrent.futures import ThreadPoolExecutor

    store = ChatThreadStore(tmp_path / "state.sqlite3")
    _note_limit(store)
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("11:27"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.claim_codex_usage_recovered, CODEX_RECOVERED_NOTICE) for _ in range(2)]
        results = [future.result() for future in futures]
    assert sorted(results) == [False, True]
    _, recovered_texts = _limit_deliveries(store)
    assert len(recovered_texts) == 1
    assert store.codex_usage_limit_active() is False


def test_failed_claim_rolls_back_and_leaves_no_partial_state(tmp_path: Path) -> None:
    """Прерванная транзакция не оставляет половину записанного лимита.

    Уведомление и метка пишутся вместе, поэтому сбой посередине не должен ни
    оставить метку без сообщения, ни повесить блокировку на базе."""
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    with pytest.raises(RuntimeError, match="boom"):
        with store._write_locked() as connection:
            connection.execute(
                "INSERT INTO owner_query_deliveries(text, created_at) VALUES(?, ?)", ("полузапись", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO operational_state(key, value) VALUES(?, '1')", (_CODEX_LIMIT_KEY,),
            )
            raise RuntimeError("boom")

    assert store.codex_usage_limit_active() is False
    limit_texts, _ = _limit_deliveries(store)
    assert limit_texts == []
    # База осталась пригодной для следующей записи.
    store.claim_codex_usage_limit("11:27", codex_limit_notice("11:27"), source_timezone_name="Europe/Moscow", local_hint="11:27")
    assert store.codex_usage_limit_active() is True


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
