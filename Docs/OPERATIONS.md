# Rick operations

Canonical entrypoint: `.venv/bin/python scripts/diagnose.py` from the project root. It is read-only and never loads `.env` or calls Telegram/Codex. Source of truth: `runtime/agentbridge.sqlite3`; logs: `runtime/logs/agentbridge.log*` (UTC, seven-day rotation); service: `rick.service`. `UNKNOWN` means no live proof. See `ARCHITECTURE.md` for delivery semantics.

For any failure: run diagnose, inspect `systemctl status rick.service` and `journalctl -u rick.service --since '1 hour ago' --no-pager`, then search log lines by `event=`, `chat_id`, `update_id`, recommendation/delivery ID. Never print `.env`, auth cache, client content, or raw model prompts.

| Symptom | Inspect | Response |
| --- | --- | --- |
| Rick silent | process status, `telegram.poll_last_log`, inbox and delivery counts, recent failures | Identify root cause first. Restart only with owner authorization or established incident procedure. |
| Polling stalled | `telegram_poll_stalled`, `telegram_poll_restart_success`, `telegram_polling_fatal` | Check network, Telegram token validity, and competing poller. Watchdog recovers a stalled updater; systemd restarts a fatal process. |
| Codex failing | recent `codex_*` failures, `codex login status` as `rick` | Repair auth/runtime only with authorized credentials. Do not switch to paid API calls silently. |
| SQLite unhealthy | `sqlite.health`, free disk, file ownership | Stop service and preserve DB before repair. Restore verified snapshot only with owner approval. |
| Disk low | `disk.free_bytes`, runtime/media/log sizes | Inspect retained media and snapshots. Do not delete DB or client documents casually. |
| Pending grows | inbox, recommendations, deliveries, reminders | Trace one ID through SQLite and events. At-least-once Telegram delivery may duplicate after a send/link crash. |
| Restart loop | `systemctl status`, `journalctl`, `startup_failed`, `process_failed` | Fix dependency/config/permission failure; `StartLimitBurst` prevents a tight loop. |
| Morning report missing | `daily_reports`, pending `owner_query_deliveries`, timezone settings | Verify due time and Telegram delivery; report creation and delivery retry are independent of Codex. |
| Crash notice missing | `operational_state`, `previous_run_unclean`, owner delivery queue | A clean SIGTERM leaves no crash notice. Unclean prior run queues a notice on next start. |
| Owner restart fails | general-task confirmation, `self_restarts`, `self_restart_launch_failed`, sudoers exact command | Test `sudo -n /usr/bin/systemctl --no-block restart rick.service` only in a scheduled window. Never broaden sudoers. |

After an approved fix run `pytest -q tests`, `compileall`, `pip check`, `git diff --check`, diagnose, and an appropriate fake acceptance path. Rick's Codex runtime stays read-only; do not modify sibling VPS projects or service files through a model turn. A future confirmed write workflow needs a deterministic resolved-path boundary, symlink checks, and its own tests before enabling writes.

Future self-maintenance design: keep the analytical provider read-only. A separate confirmed general task may hand a proposed edit to a deterministic worker. Before every write, resolve the project root and target path, reject any target outside the root (including symlink escapes), and reject service/auth/runtime paths unless explicitly in scope. Persist owner confirmation and the exact proposed diff before execution; never accept a model-provided shell command. Add traversal, symlink, duplicate-confirmation, and rollback tests before enabling this path. No such write path is enabled now.
