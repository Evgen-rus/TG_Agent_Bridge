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

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest
```
