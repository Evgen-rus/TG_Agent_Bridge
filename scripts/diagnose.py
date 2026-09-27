"""Read-only local health snapshot; never loads .env or starts network clients."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
DB = RUNTIME / "agentbridge.sqlite3"
LOGS = RUNTIME / "logs"


def _size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _stamp(line: str) -> str:
    parts = line.split()
    return parts[0].removeprefix("timestamp=") if parts and parts[0].startswith("timestamp=") else " ".join(parts[:2])


def _epoch(stamp: str) -> float | None:
    """Числовое время метки для сравнения событий; None если разобрать нельзя."""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return None


def _after_success(failures: list[str], success: str | None) -> list[str]:
    """Падения, случившиеся после последнего успеха Codex.

    Логи живут 7 суток, поэтому падение, случившееся до первого успешного
    turn, не должно вечно держать OVERALL в DEGRADED: агент уже оправился.
    """
    if not failures:
        return []
    mark = _epoch(success) if success else None
    if mark is None:
        return failures
    return [item for item in failures if (_epoch(item) or 0) > mark]


def diagnose() -> str:
    lines = ["AGENTBRIDGE DIAGNOSE (read-only)"]
    reasons = []
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                capture_output=True, text=True, timeout=3, check=False).stdout.strip() or "UNKNOWN"
    except (OSError, subprocess.TimeoutExpired):
        commit = "UNKNOWN"
    lines.append(f"app.commit={commit} python={sys.version.split()[0]}")
    if os.name == "posix":
        try:
            service = subprocess.run(["systemctl", "is-active", "rick.service"],
                                     capture_output=True, text=True, timeout=3, check=False).stdout.strip() or "UNKNOWN"
        except (OSError, subprocess.TimeoutExpired):
            service = "UNKNOWN"
        lines.append(f"service.status={service}")
        if service != "active":
            reasons.append("service_inactive" if service != "UNKNOWN" else "service_status_unknown")
    else:
        lines.append("service.status=UNKNOWN (not systemd host)")
    if not DB.is_file():
        lines.append("sqlite.exists=false sqlite.health=UNKNOWN")
        reasons.append("database_missing")
    else:
        try:
            with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
                health = conn.execute("PRAGMA quick_check").fetchone()[0]
                lines.append(f"sqlite.exists=true sqlite.health={health} sqlite.bytes={DB.stat().st_size}")
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"operational_state", "daily_reports", "operational_events"} <= tables:
                    lines.append("ops.schema=legacy (app startup will migrate)")
                    reasons.append("ops_schema_legacy")
                state = dict(conn.execute("SELECT key,value FROM operational_state")) if "operational_state" in tables else {}
                counts = {
                    "inbox": conn.execute("SELECT count(*) FROM telegram_messages WHERE processing_status IN ('pending','processing')").fetchone()[0],
                    "recommendations": conn.execute("SELECT count(*) FROM recommendations WHERE owner_message_id IS NULL").fetchone()[0],
                    "deliveries": conn.execute("SELECT count(*) FROM owner_query_deliveries WHERE owner_message_id IS NULL").fetchone()[0],
                    "reminders": conn.execute("SELECT count(*) FROM reminders WHERE sent_at IS NULL").fetchone()[0],
                }
                last_report = conn.execute("SELECT max(report_date) FROM daily_reports").fetchone()[0] if "daily_reports" in tables else None
                lines.append("queues " + " ".join(f"{key}={value}" for key, value in counts.items()))
                lines.append(f"app.run_status={state.get('run_status', 'UNKNOWN')} app.started_at={state.get('run_started_at', 'UNKNOWN')}")
                if state.get("run_status") != "running":
                    reasons.append("process_stopped" if state.get("run_status") == "stopped" else "process_status_unknown")
                lines.append(f"app.last_clean_stop={state.get('run_stopped_at', 'UNKNOWN')}")
                if state.get("run_status") == "running" and state.get("run_started_at"):
                    try:
                        uptime = int((datetime.now(timezone.utc) - datetime.fromisoformat(state["run_started_at"])).total_seconds())
                        lines.append(f"app.uptime_seconds={uptime}")
                    except ValueError:
                        lines.append("app.uptime_seconds=UNKNOWN")
                poll_at = state.get("poll_success_at")
                if poll_at:
                    try:
                        age = int((datetime.now(timezone.utc) - datetime.fromisoformat(poll_at)).total_seconds())
                        lines.append(f"telegram.poll_success_at={poll_at} telegram.poll_age_seconds={age}")
                        lines.append(f"telegram.poll_status={'RECENT' if age <= 120 and state.get('run_status') == 'running' else 'STALE' if age > 120 else 'UNKNOWN'}")
                        if state.get("run_status") == "running" and age > 120:
                            reasons.append("poll_heartbeat_stale")
                    except ValueError:
                        lines.append("telegram.poll_success_at=UNKNOWN telegram.poll_status=UNKNOWN")
                else:
                    lines.append("telegram.poll_success_at=UNKNOWN telegram.poll_status=UNKNOWN")
                lines.append(f"daily_report.last_date={last_report or 'UNKNOWN'}")
                if health != "ok":
                    reasons.append("database_integrity")
        except (OSError, sqlite3.Error) as exc:
            lines.append(f"sqlite.health=FAILED error_type={type(exc).__name__}")
            reasons.append("database_unreadable")
    log_files = sorted(LOGS.glob("agentbridge.log*"), key=lambda path: path.stat().st_mtime) if LOGS.is_dir() else []
    failures = []
    poll = None
    codex_success = None
    codex_failures = []
    poll_stalls = 0
    poll_restarts = 0
    for path in log_files[-7:]:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if any(event in line for event in ("event=telegram_poll_ok", "event=telegram_poll_restart_success", "event=telegram_polling_healthy", "event=telegram_polling_restarted")):
                    poll = _stamp(line)
                if "event=telegram_poll_stalled" in line or "event=telegram_polling_stalled" in line:
                    poll_stalls += 1
                if "event=telegram_poll_restart_success" in line or "event=telegram_polling_restarted" in line:
                    poll_restarts += 1
                if "event=codex_turn_finished" in line:
                    codex_success = _stamp(line)
                if "event=codex_turn_failed" in line:
                    codex_failures.append(_stamp(line))
                if "level=ERROR" in line or "level=CRITICAL" in line or " ERROR " in line or " CRITICAL " in line:
                    event = next((part for part in line.split() if part.startswith("event=")), "event=UNKNOWN")
                    failures.append(f"{_stamp(line)} {event}")
        except OSError:
            continue
    lines.append(f"telegram.poll_last_log={poll or 'UNKNOWN'}")
    lines.append(f"telegram.stalls_in_logs={poll_stalls} telegram.restarts_in_logs={poll_restarts}")
    lines.append(f"codex.model={os.getenv('CODEX_MODEL', 'UNKNOWN')} effort={os.getenv('CODEX_REASONING_EFFORT', 'UNKNOWN')} defaults=gpt-6-luna/xhigh (.env not read)")
    active_failures = _after_success(codex_failures, codex_success)
    lines.append(f"codex.auth_file_present={(Path.home() / '.codex' / 'auth.json').is_file()} codex.last_success={codex_success or 'UNKNOWN'}")
    lines.append(f"codex.turn_failures_in_logs={len(codex_failures)} codex.active_failures={len(active_failures)} codex.last_failure={codex_failures[-1] if codex_failures else 'NONE'}")
    if active_failures:
        # Агент молча деградирует в фолбэк-ответы, поэтому падение Codex — это DEGRADED,
        # даже когда сервис, Telegram и SQLite здоровы. Сбой до последнего успеха
        # считается историческим: агент уже отвечает нормально.
        reasons.append("codex_turn_failures")
    lines.append(f"runtime.bytes={_size(RUNTIME)} media.bytes={_size(RUNTIME / 'media')} logs.bytes={_size(LOGS)} disk.free_bytes={shutil.disk_usage(ROOT).free}")
    lines.extend(f"recent_failure {item}" for item in failures[-10:])
    lines.append("OVERALL: " + ("DEGRADED " + ",".join(reasons) if reasons else "HEALTHY (unverified fields marked UNKNOWN)"))
    return "\n".join(lines)


if __name__ == "__main__":
    print(diagnose())
