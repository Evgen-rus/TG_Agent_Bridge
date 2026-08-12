# AgentBridge architecture

This document is a compact orientation map for future Codex work. The current
code and tests are authoritative when they differ from this summary.

## Flow

```text
Telegram long polling
  -> telegram.bot: filter people/text/known chat and batch per chat
  -> application: deduplicate updates, load chat context, coordinate one turn
  -> AgentProvider
  -> CodexProvider: start or resume the chat's Codex thread
  -> telegram.formatter
  -> OWNER_CHAT_ID only
```

`drop_pending_updates=True` discards messages accumulated while the process was
offline. The batching window is `MESSAGE_BATCH_SECONDS` (20 seconds by default).
Messages from different Telegram chats are never placed in one batch.

## Components

- `agentbridge/main.py`: composition root and polling lifecycle.
- `agentbridge/settings.py`: `.env` configuration.
- `agentbridge/telegram/`: Telegram adapter, per-chat batching, filters, and
  owner-message formatting.
- `agentbridge/application.py`: use-case orchestration and batch normalization.
- `agentbridge/agents/base.py`: provider boundary.
- `agentbridge/agents/codex.py`: official `openai-codex` implementation and
  structured Codex output.
- `agentbridge/chats/loader.py`: loads `chats/*/config.yaml` plus sibling
  `wiki.md` into a registry keyed by Telegram chat ID.
- `agentbridge/storage/sqlite.py`: SQLite thread mapping and processed update IDs.
- `agentbridge/logging.py`: process-wide secret redaction.

## Memory

There are two durable context layers:

1. `chats/<name>/wiki.md` contains stable, manually maintained knowledge and is
   included in every Codex turn for that chat.
2. SQLite stores `telegram_chat_id -> codex_thread_id`; later batches resume the
   same Codex thread, which provides conversational continuity.

SQLite also stores processed Telegram update IDs for duplicate suppression. The
20-second pending batch exists only in process memory and is lost on shutdown.
Telegram message history itself is not stored locally.

## Invariants

- Suggest-only: never reply automatically to a monitored/client chat.
- One Telegram chat maps to one independent Codex thread and wiki.
- Wiki is read-only at runtime.
- Bot-authored messages and commands do not invoke Codex.
- A successful batch is marked processed only after the Codex turn and thread
  mapping are saved.
- Tokens and credentials never appear unredacted in logs or tracked files.
- `AgentProvider` is the only application-to-agent dependency; Telegram code
  must not depend on Codex SDK details.

