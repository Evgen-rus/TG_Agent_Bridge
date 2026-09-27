from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from agentbridge.agents.codex import CodexProvider, is_usage_limit_error, limit_reset_hint
from agentbridge.application import AgentBridgeApplication
from agentbridge.storage.sqlite import (
    CODEX_RECOVERED_NOTICE,
    _CODEX_LIMIT_KEY,
    _CODEX_LIMIT_REASON_KEY,
    ChatThreadStore,
    codex_limit_notice,
    codex_limit_reset_local,
)
from agentbridge.logging import redact_secrets
from agentbridge.telegram.bot import due_report_windows
from agentbridge.telegram.polling import PollingHeartbeat
from scripts import diagnose as health
from tests.test_application import QueryProvider


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
    assert "фактов: 1; правил: 1; опыта: 1" in text
    assert "ошибок 1" in text


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
    limit_error = SimpleNamespace(message="You've hit your usage limit. try again at 11:27 AM.")
    provider = CodexProvider(on_usage_limit=lambda hint: None, on_usage_recovered=lambda: None)
    provider._note_failure(limit_error)
    provider._note_failure(limit_error)
    provider._note_failure(limit_error)
    provider._note_success()
    provider._note_success()
    provider._note_failure(limit_error)
    provider._note_success()
    assert provider._usage_exhausted is False


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


def test_other_failures_do_not_touch_the_limit_notice() -> None:
    provider = CodexProvider(on_usage_limit=lambda hint: None, on_usage_recovered=lambda: None)
    provider._note_failure(RuntimeError("connection reset by peer"))
    assert provider._usage_exhausted is False


def test_reset_time_is_translated_from_session_zone() -> None:
    # Codex печатает время в зоне сессии. На этом VPS сессия идёт по Москве,
    # поэтому 11:27 — это 15:27 по Новосибирску, а не 18:27: раньше здесь
    # ошибочно считали, что это UTC, и время уезжало на три часа.
    def local(zone: str, source: str = "Europe/Moscow") -> str:
        return codex_limit_reset_local("11:27", zone, session_timezone_name=source)

    assert local("Asia/Novosibirsk") == "15:27 Novosibirsk (UTC+07:00)"
    assert local("Europe/Moscow") == "11:27 Moscow (UTC+03:00)"
    assert local("Asia/Yekaterinburg") == "13:27 Yekaterinburg (UTC+05:00)"
    # Если сессия Codex поедет в UTC, пересчёт последует за ней.
    assert local("Asia/Novosibirsk", "UTC") == "18:27 Novosibirsk (UTC+07:00)"
    # Переход через полночь: 23:59 по Москве — это уже 03:59 следующих суток.
    assert codex_limit_reset_local("23:59", "Asia/Novosibirsk", session_timezone_name="Europe/Moscow") == "03:59 Novosibirsk (UTC+07:00)"
    assert codex_limit_reset_local("00:30", "Asia/Novosibirsk", session_timezone_name="Europe/Moscow") == "04:30 Novosibirsk (UTC+07:00)"
    # Негодные данные не ломают уведомление, а остаются как есть.
    assert codex_limit_reset_local("", "Asia/Novosibirsk", session_timezone_name="Europe/Moscow") == ""
    assert codex_limit_reset_local("мусор", "Asia/Novosibirsk", session_timezone_name="Europe/Moscow") == "мусор"
    assert codex_limit_reset_local("11:27", "Not/AZone", session_timezone_name="Europe/Moscow") == "11:27"


def test_saved_reset_hint_is_readable_and_cleared(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "state.sqlite3")
    store.note_codex_usage_limit("11:27 UTC")
    assert store.codex_usage_limit_reason() == "11:27 UTC"
    # Повторный отказ обновляет время, а не теряет его.
    store.note_codex_usage_limit("14:27 UTC")
    assert store.codex_usage_limit_reason() == "14:27 UTC"
    store.clear_operational_state(_CODEX_LIMIT_REASON_KEY)
    assert store.codex_usage_limit_reason() == ""


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

    store.note_codex_usage_limit("11:27 UTC")
    store.queue_operational_notice(_CODEX_LIMIT_KEY, codex_limit_notice("11:27 UTC"))
    limited = await service.handle_owner_query("Ну что там?")
    assert "Лимит Codex исчерпан" in limited.text
    assert "11:27 UTC" in limited.text
    assert "Ну что там?" in limited.text
    assert "Уточните" not in limited.text
    # Известный лимит не должен стоить ещё одной попытки обращения к Codex.
    assert provider.owner_threads_created == attempts_without_limit
