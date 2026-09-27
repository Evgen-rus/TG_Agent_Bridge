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


def _is_recent(stamp: str, success: str | None) -> bool:
    """Случилось ли событие после последнего успешного turn Codex.

    Метки, которые не разобрались, считаем свежими: иначе verdict рисковал бы
    объявить реальный лимит устаревшей меткой только из-за формата записи в
    логе."""
    mark = _epoch(success) if success else None
    if mark is None:
        return True
    value = _epoch(stamp)
    return True if value is None else value > mark


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


def _codex_limit_lines(conn: sqlite3.Connection) -> list[str]:
    """Состояние лимита Codex из одного снимка SQLite.

    Нужно, чтобы по diagnose было видно главное: «Codex реально сломан» или
    «метка лимита устарела и пора делать контрольную попытку». Фонового опроса
    модели здесь нет — читаются только уже сохранённые метки времени.
    Логика решения продублирована намеренно: скрипт не импортирует
    приложение, чтобы остаться независимым от её изменений."""
    keys = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT key, value FROM operational_state WHERE key LIKE 'codex_usage_limit%'"
        )
    }
    active = "codex_usage_limit:notice" in keys
    reset_hint = keys.get("codex_usage_limit:reset_hint") or "UNKNOWN"
    reset_at = keys.get("codex_usage_limit:reset_at_utc") or "UNKNOWN"
    seen_at = keys.get("codex_usage_limit:seen_at") or "UNKNOWN"
    lines = [
        f"codex.limit_active={str(active).lower()} codex.limit_reset_hint={reset_hint} "
        f"codex.limit_reset_at_utc={reset_at} codex.limit_seen_at={seen_at}"
    ]
    if not active:
        return lines, False
    now = datetime.now(timezone.utc)
    reset_dt = _parse_stamp(reset_at)
    seen_dt = _parse_stamp(seen_at)
    if reset_dt is not None:
        if now >= reset_dt:
            verdict = "READY (reset_time_reached) — следующий запрос владельца проверит Codex"
        else:
            verdict = f"WAIT (before_reset_time, {int((reset_dt - now).total_seconds())}s)"
    elif seen_dt is not None:
        # Интервал повторной попытки без известного сброса держим тем же, что
        # и приложение: CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN.
        left = int(seen_dt.timestamp() + 30 * 60 - now.timestamp())
        verdict = (
            "READY (cooldown_elapsed) — следующий запрос владельца проверит Codex"
            if left <= 0
            else f"WAIT (cooldown, {left}s)"
        )
    else:
        verdict = "READY (no_probe_time_known) — время сброса неизвестно"
    lines.append(f"codex.limit_recovery_probe={verdict}")
    return lines, True


def _parse_stamp(stamp: str) -> datetime | None:
    if not stamp or stamp == "UNKNOWN":
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _codex_verdict(limit_active: bool, active_failures: list[str], codex_success: str | None, limit_failures: list[str] | None = None) -> str:
    """Одноразборный вывод: лимит активен, метка устарела или Codex сломан.

    Различать «метка лимита устарела» и «Codex сломан» можно только по
    доказательствам, поэтому verdict не угадывает:

    - `active_usage_limit` — метка активна, есть свежий отказ именно по лимиту,
      и успеха после него нет. Это обычное состояние исчерпанного лимита, а не
      поломка: следующий запрос владельца его проверит и снимет метку.
    - `stale_limit_flag` — метка активна и доказанно пережила восстановление:
      после отказа по лимиту в логах есть успешный turn. Единственное
      состояние, где метку можно назвать устаревшей.
    - `active_limit_unknown` — метка активна, а доказательств в логах нет
      вовсе: отказа по лимиту не видно либо успех был раньше него. Состояние
      не диагностируется, а не считается поломкой.
    - `real_failure` — свежие отказы есть, а про лимит ничего не известно.
    - `ok` — свежих отказов нет.

    Раньше `limit_active` с пустым списком отказов сразу давал
    `stale_limit_flag`, хотя успеха после лимита в логах могло не быть вовсе:
    отсутствие улик выдавалось за доказательство."""
    limit_failures = limit_failures or []
    if active_failures:
        recent_limit = [item for item in limit_failures if _is_recent(item, codex_success)]
        if limit_active and recent_limit:
            return "active_usage_limit (no success after the limit failure; next owner request probes Codex)"
        return f"real_failure ({len(active_failures)} failed turns after the last success)"
    if limit_active:
        if not limit_failures:
            return "active_limit_unknown (limit marked active, but no limit failure in the last 7 days of logs)"
        if not codex_success:
            return "active_limit_unknown (limit marked active, but no successful Codex turn in the last 7 days of logs)"
        newest_limit = max((_epoch(item) or 0.0 for item in limit_failures), default=None)
        if newest_limit is None or (_epoch(codex_success) or 0.0) <= newest_limit:
            return "active_usage_limit (limit marked active, last success predates the limit failure; next owner request probes Codex)"
        return "stale_limit_flag (successful turn after the limit failure — clearing on next turn)"
    return "ok"


def diagnose() -> str:
    lines = ["AGENTBRIDGE DIAGNOSE (read-only)"]
    reasons = []
    limit_active = False
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
                limit_lines, limit_active = _codex_limit_lines(conn)
                lines.extend(limit_lines)
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
    codex_limit_failures = []
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
                # Отказ именно по лимиту отличается от прочих поломок: по нему
                # verdict понимает, что метка лимита не устарела, а отвечает
                # своему назначению.
                if "event=codex_usage_limit_exhausted" in line:
                    codex_limit_failures.append(_stamp(line))
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
    # Главный вопрос по лимиту: активен ли лимит, устарела ли метка или Codex сломан.
    # Ответ строится из двух независимых источников — сохранённой метки и живых логов.
    lines.append(f"codex.limit_failures_in_logs={len(codex_limit_failures)}")
    lines.append(f"codex.verdict={_codex_verdict(limit_active, active_failures, codex_success, codex_limit_failures)}")
    lines.append(f"runtime.bytes={_size(RUNTIME)} media.bytes={_size(RUNTIME / 'media')} logs.bytes={_size(LOGS)} disk.free_bytes={shutil.disk_usage(ROOT).free}")
    lines.extend(f"recent_failure {item}" for item in failures[-10:])
    lines.append("OVERALL: " + ("DEGRADED " + ",".join(reasons) if reasons else "HEALTHY (unverified fields marked UNKNOWN)"))
    return "\n".join(lines)


if __name__ == "__main__":
    print(diagnose())
