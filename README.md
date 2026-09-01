# AgentBridge

Minimal suggest-only bridge from monitored Telegram chats to a persistent Codex
thread. Suggestions go only to the configured owner chat; the bot never replies
to monitored chats.

Incoming Telegram messages are saved to SQLite first, then analysed as a short
episode of the current situation. After a restart the bot ingests the Telegram
backlog instead of dropping it, rebuilds the picture from stored history, and
notifies the owner only about the current outcome.

## Configure

1. Copy `.env.example` to `.env`.
2. Set `TELEGRAM_BOT_TOKEN` and `OWNER_CHAT_ID`.
3. For voice message transcription set `OPENAI_API_KEY`; `TRANSCRIPTION_MODEL`
   defaults to `gpt-4o-mini-transcribe`.
4. Copy `chats/example` to a meaningful directory, set the monitored numeric
   `telegram_chat_id`, and replace `wiki.md` with that chat's stable context.
   Loaded chats receive the compact `knowledge/leadgenbureau` pack by default.
   Set `knowledge_pack: none` to opt out. Only `core.md` is injected each turn.
5. Remove the example directory or change its placeholder chat ID.

Client recommendations default to `gpt-5.6-luna` with reasoning effort `xhigh`.
The internal Owner contour has separate settings and defaults to `gpt-5.6-sol`
with the minimum reasoning effort `low`. Change them in `.env` without editing
code, for example when you want to reduce usage:

```dotenv
OWNER_CODEX_MODEL=gpt-5.6-terra
OWNER_CODEX_REASONING_EFFORT=none
```

`OWNER_CODEX_MODEL` can also be set back to `gpt-5.6-luna`. The official Python
SDK reuses existing Codex authentication. To start browser login from Python
when no account is available:

```powershell
.\.venv\Scripts\python.exe -c "from openai_codex import Codex; c=Codex(); h=c.login_chatgpt(); print(h.auth_url); print(h.wait().success); c.close()"
```

Never commit `.env` or Codex authentication files.

`MESSAGE_BATCH_SECONDS` controls the live per-chat collection window and
defaults to 20 seconds. Human messages received in the same monitored chat
during that window, including messages from different people, are analysed
together as one episode. Separate chats always use separate batches.

After downtime, polling starts with live analysis off. Incoming backlog is
saved to SQLite first. `CATCHUP_IDLE_SECONDS` (2 by default) waits until that
stream goes quiet, then catch-up processes the pending sequence. A large
backlog is split into ordered episodes of `CATCHUP_EPISODE_SIZE` messages.
Only the last episode may notify the owner. Live debounce begins after catch-up.

The globally installed `codex` command is optional for this project: the
`openai-codex` package includes its compatible runtime. Authentication is still
required before a real suggestion can be generated.

## Run

```powershell
.\.venv\Scripts\python.exe -m agentbridge.main
```

The SQLite mapping is created at `runtime/agentbridge.sqlite3`. It stores
Telegram history, one Codex thread ID per monitored chat, compact `chat_state`,
recommendations, rules, and memory. Client photos and files are downloaded only
for the current episode into `MEDIA_DIR` (default `runtime/media`), then deleted.
`MEDIA_TTL_SECONDS` (3600) is the leftover-file safety net. Telegram `file_id`
stays in SQLite so the bot can fetch the same file again if needed.

Voice messages are supported. Each incoming voice note is stored first, then
transcribed in the background with `TRANSCRIPTION_MODEL` (requires
`OPENAI_API_KEY`) while the per-chat batch window runs; the transcript is saved
to SQLite and sent to Codex as `[голосовое] текст` instead of an audio file.
If transcription fails or hears no speech, a placeholder text is used so the
batch is never blocked. Without `OPENAI_API_KEY` voice notes stay pending as
plain attachments and everything else works as before.

A voice note sent by the owner in the owner chat (typically as a reply to a bot
recommendation) is transcribed on the fly and then handled exactly like typed
text: feedback and question answers work over voice. A standalone owner voice
note invokes the assistant when its transcript begins with `Рик` or `Агент`.
The same leading names invoke it in an ordinary owner text message. Telegram
media downloads use bounded retries; replying to a voice-download error keeps
the original bot-message link for the repeated voice note.

For predictable suggest-only operation, startup no longer drops Telegram
updates. Successfully processed update IDs are stored in the same SQLite
database, so Telegram retries are not sent to Codex twice. Messages authored by
bot accounts are ignored.

## Owner learning

Use a separate service group as `OWNER_CHAT_ID`. The bot speaks there as a
second pilot: it can propose a client reply, ask you one missing fact, or just
point out a risk. It does not jump into ordinary conversation.

The agent is invoked in the owner group when:

- a person replies to a bot recommendation or question;
- a person explicitly mentions/tags the bot, for example `@agent что сейчас
  происходит с Татьяной?`;
- a person replies to a “new group without wiki” card after the bot became
  admin, or after the first message from a group where it was already admin.

Telegram cannot list every group the bot is already in. A quiet group with no
new messages stays undiscovered until someone writes or membership changes.

Any human member can reply to a suggestion with a correction in ordinary
language. Codex shows how it understood the correction; **Да, применить**
confirms it and **Нет, уточнить** asks for clarification. Rules apply to that
client by default. Global scope requires explicit wording such as "for all
clients".

Use `/rules` in the owner group to inspect active rules and `/undo` to deactivate
the most recently confirmed active rule. After startup those two commands appear
only in the owner chat Telegram command menu (the `/` list, or the Menu button
in a private owner chat). Client chats do not get this menu. Corrected
suggestions still go only to the owner group and are never sent to a client chat.

If the bot asks a clarifying question, reply to that question. The answer is
applied to the original client chat and may be offered as confirmable memory.

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

In the owner group you can add common memory without choosing a client. Write a
new message (no reply and no @mention needed):

```text
Общий контекст: фраза про отдел маркетинга утверждена для робота.
```

The bot asks for confirmation, then stores the fact for every connected chat.
`Контекст:` and `Контекст проекта:` still need a reply to a bot recommendation.
Messages from the configured internal LeadRecord participants are not answered,
but their recent text is retained as local context for later client messages.

Diagnostic logs are written to `runtime/logs/agentbridge.log`, rotate daily,
and retain seven days by default. They contain event identifiers and processing
stages, not Telegram message bodies. `LOG_DIR` and `LOG_RETENTION_DAYS` override
the defaults.

Startup `getMe` retries `TELEGRAM_BOOTSTRAP_RETRIES` times (5 by default) if
Telegram times out through a proxy. `0` keeps the old fail-fast behaviour.

Long polling has a separate liveness guard. Every `getUpdates` call updates a
monotonic heartbeat, including empty responses from quiet chats.
`TELEGRAM_POLL_HARD_TIMEOUT_SECONDS` (30 by default) cancels a single stuck
request. The watchdog checks every `TELEGRAM_POLL_WATCHDOG_SECONDS` (15) and,
after `TELEGRAM_POLL_STALL_SECONDS` (90) without polling progress, restarts the
Telegram updater without dropping pending updates. A restart has
`TELEGRAM_POLL_RESTART_TIMEOUT_SECONDS` (30) to finish. If recovery fails, the
process exits with an error instead of remaining falsely healthy; run it under
a supervisor configured to restart failed processes. Healthy polling is logged
at most once every five minutes, while stalls and restarts are logged
immediately.

Recommendations are written to SQLite before Telegram delivery. If Telegram is
temporarily unavailable, delivery is retried every `DELIVERY_RETRY_SECONDS`
seconds and pending recommendations are retried again after application restart.
Delivery is at-least-once: if Telegram accepts a message but the local database
update fails, the same recommendation can be sent twice.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest
```
