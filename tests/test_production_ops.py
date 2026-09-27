from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.logging import redact_secrets
from agentbridge.telegram.bot import due_report_windows
from agentbridge.telegram.polling import PollingHeartbeat
from scripts import diagnose as health


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
    assert "OVERALL: DEGRADED process_status_unknown" in output


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
