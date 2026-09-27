# Rick on Ubuntu (manual deployment)

Target: one Ubuntu VPS, Python virtualenv, SQLite, `rick.service`, no Docker. Commands below run on the VPS; replace the repository URL if necessary. Do not deploy until the owner schedules a maintenance window and identifies the current live bot process: Telegram permits only one `getUpdates` poller per token.

## Prepare

1. Provide Ubuntu with outbound HTTPS to Telegram/OpenAI, enough disk for `runtime/media`, and daily VPS snapshots. Install `git`, `python3`, `python3-venv`, `python3-pip`, `tzdata`, and `sudo` using apt. Ubuntu 22.04 ships Python 3.10; use an isolated Python 3.12 under the `rick` home rather than replacing the system Python.
2. Create a dedicated unprivileged `rick` user with home `/home/rick`. Clone `https://github.com/Evgen-rus/TG_Agent_Bridge.git` as that user into `/home/rick/TG_Agent_Bridge`.
3. As `rick` on Ubuntu 22.04, run `python3 -m venv ~/.bootstrap-venv`, `~/.bootstrap-venv/bin/python -m pip install uv`, `~/.bootstrap-venv/bin/uv python install 3.12`, and `~/.bootstrap-venv/bin/uv venv --seed --python 3.12 .venv`. On hosts with Python 3.11 or newer already available, `python3 -m venv .venv` is sufficient. Then run `.venv/bin/python -m pip install -r requirements.txt`. Check `.venv/bin/python -m pip check` and `.venv/bin/python -m pytest -q tests`.
4. Copy `.env.example` to `.env`, set the Telegram token and owner chat ID privately, and set the non-secret options. Keep `DAILY_REPORT_ENABLED=true`, `DAILY_REPORT_TIME=07:30`, `DAILY_REPORT_TIMEZONE=Europe/Moscow`. Set `chmod 600 .env`; never commit or print its contents.
5. Create `runtime/logs` and `runtime/media`, owned by `rick`, with restrictive permissions (`chmod 700 runtime runtime/logs runtime/media`). `runtime/` and `.env` are ignored by Git. `chats/*` is also local and ignored; copy actual `config.yaml` and `wiki.md` directories from the old host. Copy any other local knowledge files those chat configs refer to.
6. Stop the old bot before starting Rick on the VPS. Transfer `runtime/agentbridge.sqlite3` plus retained files under `runtime/media`. A consistent SQLite transfer is made while the old process is stopped; if not possible, use SQLite's backup API rather than copying a live file. Do not transfer stale `runtime/pytest-tmp*` or logs unless needed for investigation. Keep a separate snapshot before schema changes.

## Codex authentication

Run Codex authentication as the **rick** Unix user so the service sees the same home and auth cache. The Python dependency bundles the CLI. Locate it with `.venv/bin/python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())'`; run that printed executable with `login --device-auth`, then `login status`. Enable device-code login in ChatGPT settings if needed. Device code login and headless fallback are described in the [official OpenAI authentication guide](https://learn.chatgpt.com/docs/auth). Never paste a device code, `auth.json`, or tokens into logs, chat, or Git. If using a copied auth cache, protect `~/.codex/auth.json` with mode 600 and ownership `rick`. Do one explicitly authorized test turn only after service startup.

## Service

1. Review `deploy/systemd/rick.service`; its project path and user must match the actual checkout. Copy it to `/etc/systemd/system/rick.service` with root ownership, then `sudo systemctl daemon-reload`.
2. For confirmed owner self-restart, install `deploy/systemd/rick-sudoers.example` as `/etc/sudoers.d/rick-restart` with owner root and mode 0440. Validate with `sudo visudo -cf /etc/sudoers.d/rick-restart`. It permits only `/usr/bin/systemctl --no-block restart rick.service`; no shell or arbitrary service name.
3. Run `sudo systemctl enable --now rick.service`. Check `systemctl status rick.service`, `journalctl -u rick.service -n 100 --no-pager`, and `sudo -u rick /home/rick/TG_Agent_Bridge/.venv/bin/python /home/rick/TG_Agent_Bridge/scripts/diagnose.py`.
4. Validate owner-only delivery, real Telegram polling heartbeat, one monitored-chat fake or authorized live event, pending queues, Codex authentication, and the next 07:30 Moscow report. `daily_reports` in SQLite records the date and delivery ID. Restart gracefully with `sudo systemctl restart rick.service`; this must not create a crash notice. An unclean process exit should create one durable notice after recovery.

## Updates and rollback

Before updating, inspect changes, stop the service, and snapshot the SQLite file. Pull code, install requirements if changed, run tests and diagnose, then start and check the service. For rollback, stop, restore the previous Git revision and matching SQLite snapshot if a schema change occurred, then start and diagnose. `git pull` does not touch ignored `.env`, `runtime/`, `chats/*`, or auth under the service user's home. After VPS reboot, `systemctl is-enabled rick.service` and `systemctl status rick.service` show whether startup succeeded; diagnose and inspect the owner delivery queue.

Do not add an application-level daily SQLite backup scheduler; external VPS snapshots are the backup policy. Any destructive migration requires a separate maintenance decision.
