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

OWNER_CHAT_ID reply to bot recommendation
  -> isolated Codex feedback interpretation
  -> human yes/no confirmation
  -> versioned SQLite rule (+ optional current suggestion regeneration)
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
- `agentbridge/storage/sqlite.py`: SQLite threads, processed updates,
  pending/delivered recommendation links, learning drafts, and versioned rules.
- `agentbridge/logging.py`: secret redaction and seven-day daily diagnostic
  file rotation.

## Memory

There are two durable context layers:

1. `chats/<name>/wiki.md` contains stable, manually maintained knowledge and is
   included in every Codex turn for that chat.
2. SQLite stores `telegram_chat_id -> codex_thread_id`; later batches resume the
   same Codex thread, which provides conversational continuity.

SQLite also stores processed Telegram update IDs for duplicate suppression. The
20-second pending batch exists only in process memory and is lost on shutdown.
Telegram message history itself is not stored locally.

Recommendations are persisted before owner delivery. A missing owner message ID
means the recommendation is pending; the Telegram adapter retries pending rows
periodically and on startup. This is at-least-once delivery, so a crash between
Telegram acceptance and SQLite acknowledgement can produce a duplicate.

Confirmed rules form a third durable context layer and are included in future
Codex turns for the matching chat. Feedback is interpreted in a separate Codex
thread and cannot affect the client thread until confirmed. A newer rule with
the same semantic conflict key supersedes the old version. Global scope also
requires explicit global wording in the owner's feedback.

Codex returns `should_notify` for every client turn. Confirmed rules can
therefore suppress an owner recommendation entirely (for example, while the
last message is from an internal employee) without losing thread continuity or
duplicate-processing state.

## Invariants

- Suggest-only: never reply automatically to a monitored/client chat.
- One Telegram chat maps to one independent Codex thread and wiki.
- Wiki is read-only at runtime.
- Bot-authored messages and commands do not invoke Codex.
- A successful batch is marked processed only after the Codex turn and thread
  mapping are saved.
- Tokens and credentials never appear unredacted in logs or tracked files.
- Only replies to bot recommendations in `OWNER_CHAT_ID` initiate learning;
  ordinary owner-group conversation is ignored.
- `AgentProvider` is the only application-to-agent dependency; Telegram code
  must not depend on Codex SDK details.
