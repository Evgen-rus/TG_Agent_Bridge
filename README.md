# AgentBridge

Minimal suggest-only bridge from monitored Telegram chats to a persistent Codex
thread. Suggestions go only to the configured owner chat; the bot never replies
to monitored chats.

## Configure

1. Copy `.env.example` to `.env`.
2. Set `TELEGRAM_BOT_TOKEN` and `OWNER_CHAT_ID`.
3. Copy `chats/example` to a meaningful directory, set the monitored numeric
   `telegram_chat_id`, and replace `wiki.md` with that chat's stable context.
4. Remove the example directory or change its placeholder chat ID.

Codex defaults to `gpt-5.6-sol` with reasoning effort `medium`. The official
Python SDK reuses existing Codex authentication. To start browser login from
Python when no account is available:

```powershell
.\.venv\Scripts\python.exe -c "from openai_codex import Codex; c=Codex(); h=c.login_chatgpt(); print(h.auth_url); print(h.wait().success); c.close()"
```

Never commit `.env` or Codex authentication files.

`MESSAGE_BATCH_SECONDS` controls the per-chat collection window and defaults to
20 seconds. Human messages received in the same monitored chat during that
window, including messages from different people, are sent to Codex together as
one turn. Separate chats always use separate batches.

The globally installed `codex` command is optional for this project: the
`openai-codex` package includes its compatible runtime. Authentication is still
required before a real suggestion can be generated.

## Run

```powershell
.\.venv\Scripts\python.exe -m agentbridge.main
```

The SQLite mapping is created at `runtime/agentbridge.sqlite3`. Each monitored
Telegram chat gets one stored Codex thread ID, which is resumed after restart.

For predictable suggest-only operation, startup discards Telegram updates that
accumulated while the application was offline. Successfully processed update
IDs are stored in the same SQLite database, so Telegram retries are not sent to
Codex twice. Messages authored by bot accounts are ignored.

## Owner learning

Use a separate service group as `OWNER_CHAT_ID`. Any human member can reply to
a suggestion with a correction in ordinary language. Codex shows how it
understood the correction; **Да, применить** confirms it and **Нет, уточнить**
asks for clarification. Rules apply to that client by default. Global scope
requires explicit wording such as "for all clients".

Use `/rules` in the owner group to inspect active rules and `/undo` to deactivate
the most recently confirmed active rule. Corrected suggestions still go only to
the owner group and are never sent to a client chat.

### Context memory

Reply to an owner recommendation with one of these prefixes to prepare a
confirmable memory entry:

```text
Контекст: факт, важный только для этого чата
Контекст проекта: факт для связанных чатов одного проекта
Общий контекст: факт, применимый во всех подключённых чатах
```

The bot asks for confirmation before saving the entry. Chat memory stays within
that chat; project memory is available only to chats with the same
`memory_project` in `config.yaml`; common memory is available everywhere.
Messages from the configured internal LeadRecord participants are not answered,
but their recent text is retained as local context for later client messages.

Diagnostic logs are written to `runtime/logs/agentbridge.log`, rotate daily,
and retain seven days by default. They contain event identifiers and processing
stages, not Telegram message bodies. `LOG_DIR` and `LOG_RETENTION_DAYS` override
the defaults.

Recommendations are written to SQLite before Telegram delivery. If Telegram is
temporarily unavailable, delivery is retried every `DELIVERY_RETRY_SECONDS`
seconds and pending recommendations are retried again after application restart.
Delivery is at-least-once: if Telegram accepts a message but the local database
update fails, the same recommendation can be sent twice.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest
```
