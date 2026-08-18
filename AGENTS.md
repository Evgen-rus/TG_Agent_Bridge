# AGENTS.md

## Project

AgentBridge is a suggest-only bridge from monitored Telegram chats to Codex.
It stores incoming Telegram messages in SQLite first, batches them per chat
(live debounce or catch-up after restart), asks Codex for a compact situation
assessment, and sends the result only to the configured owner chat. The model
may reply, ask the owner, observe, or take no action.

Read `ARCHITECTURE.md` before changing message flow, persistence, provider, or
Telegram behavior. The current code and tests are the source of truth; update
the architecture file when those contracts materially change.

## Required behavior

- Never send an automatic reply to a client/monitored chat. The only outbound
  destination is `OWNER_CHAT_ID`; it may equal a monitored chat only for an
  explicit temporary test setup.
- Keep Telegram isolated from Codex through `AgentProvider`.
- Keep one persistent Codex thread per monitored Telegram chat.
- Load only that chat's `config.yaml` and `wiki.md`; never mix chat contexts.
- Do not modify wiki files automatically.
- Ignore bot-authored messages, Telegram commands, unknown chats, and already
  processed update IDs.
- Persist relevant Telegram updates before model processing. Do not mark an
  inbox row processed until the episode succeeds; a model failure must leave it
  pending.
- After restart, ingest Telegram backlog (`drop_pending_updates=False`) and
  process pending SQLite rows as catch-up. Do not flood the owner with stale
  per-message recommendations.
- Treat SQLite history and `chat_state` as the durable situation source. The
  Codex thread is continuity only.
- Preserve the per-chat batching behavior and keep different chats independent.
- In the owner chat, invoke the agent only on reply-to-bot or an explicit
  mention/tag.
- Never log or commit secrets. Telegram tokens must remain redacted in logs.

## Change discipline

- Prefer the smallest change that satisfies the request and reuses existing
  modules and standard-library facilities.
- Do not add services, frameworks, ORM/migration tooling, queues, frontend, or
  deployment infrastructure unless the user explicitly expands scope.
- Do not use subagents by default. Use them only when the user explicitly asks
  or when a clearly independent, substantial workstream materially benefits.
- Preserve UTF-8 for Russian text.
- Do not read or print secret values from `.env`.
- Do not perform real Telegram or Codex calls unless the task explicitly needs
  live verification; local tests must use fakes.

## Verification

Run after code changes:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q agentbridge tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

For Telegram/Codex integration changes, also verify the affected acceptance
path with fakes: monitored-chat selection, durable ingest, live/catch-up
batching, start/resume thread, SQLite persistence, owner-only delivery,
duplicate suppression, bot-message filtering, owner mention/reply, ASK_OWNER
linking, and secret redaction.

## Runtime

```powershell
.\.venv\Scripts\python.exe -m agentbridge.main
```

Runtime state belongs under `runtime/`. Secrets belong only in `.env`; document
new non-secret settings in `.env.example` and `README.md`.
