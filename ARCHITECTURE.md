# AgentBridge architecture

This document is a compact orientation map for future Codex work. The current
code and tests are authoritative when they differ from this summary.

## Flow

```text
Telegram long polling (drop_pending_updates=False)
  -> persist each relevant update in SQLite (durable inbox/history)
  -> live debounce per chat, or catch-up after restart
  -> application: episode from stored messages, context pack, one model turn
  -> AgentProvider
  -> CodexProvider: start or resume the chat's Codex thread
  -> update chat_state
  -> telegram.formatter
  -> OWNER_CHAT_ID only
    action: reply | ask_owner | observe | no_action

OWNER_CHAT_ID
  -> reply to a bot recommendation: correction / learning / memory
  -> reply to a proactive question: fill the knowledge gap
  -> mention/tag of the bot: assistant query
  -> ordinary human conversation is ignored
```

Telegram updates accumulated while the process was offline are ingested, then
processed as catch-up episodes. The batching window is `MESSAGE_BATCH_SECONDS`
(20 seconds by default). `CATCHUP_IDLE_SECONDS` waits for the startup backlog
to land in SQLite before catch-up runs. Messages from different Telegram chats
are never placed in one episode.

A large backlog is split into ordered episodes (`CATCHUP_EPISODE_SIZE`). Only
the final episode may notify the owner, so stale early questions do not create
a series of outdated recommendations.

## Components

- `agentbridge/main.py`: composition root and polling lifecycle.
- `agentbridge/settings.py`: `.env` configuration.
- `agentbridge/telegram/`: Telegram adapter, persist-first ingest, per-chat
  batching, owner mention/reply interface, and owner-message formatting.
- `agentbridge/application.py`: use-case orchestration, context pack, catch-up,
  chat_state updates, and owner assistant queries.
- `agentbridge/agents/base.py`: provider boundary and action types.
- `agentbridge/agents/codex.py`: official `openai-codex` implementation and
  structured Codex output.
- `agentbridge/chats/loader.py`: loads `chats/*/config.yaml` plus sibling
  `wiki.md` into a registry keyed by Telegram chat ID.
- `agentbridge/storage/sqlite.py`: durable Telegram history, threads, chat
  state, processed updates, pending/delivered recommendation links, learning
  drafts, versioned rules, memory, owner questions, and experience.
- `agentbridge/logging.py`: secret redaction and seven-day daily diagnostic
  file rotation.

## Memory

Durable context layers, in order of authority:

1. SQLite Telegram history for monitored chats. This is the source of truth for
   what was said. Codex thread is continuity only.
2. `chats/<name>/wiki.md` contains stable, manually maintained knowledge and is
   included in every model turn for that chat.
3. `chat_state` is a compact working snapshot (facts, decisions, commitments,
   open questions, next step). It is updated after a successful episode.
4. Confirmed memory entries have `fact` / `decision` / `commitment` /
   `preference` / `open_question` / `rule` / `assumption` / `experience` kinds
   and an explicit `chat`, `project`, or `global` scope.
5. Confirmed learning rules and recent confirmed experience are added to the
   context pack when relevant.

Before a model turn the application builds a compact context pack: wiki, current
`chat_state`, recent history, the current episode, confirmed memory, rules, and
recent experience. Do not send the whole archive.

SQLite also stores recent internal messages from the configured LeadRecord
participants for their original chat. Those messages never create an owner
recommendation by themselves, but they are part of mixed episodes and later
local context.

Recommendations are persisted before owner delivery. A missing owner message ID
means the recommendation is pending; the Telegram adapter retries pending rows
periodically and on startup. This is at-least-once delivery, so a crash between
Telegram acceptance and SQLite acknowledgement can produce a duplicate.

A newer rule with the same semantic conflict key supersedes the old version.
Global scope also requires explicit global wording in the owner's feedback.

`ASK_OWNER` is used when a needed fact is missing. The question is sent only to
`OWNER_CHAT_ID` and linked back to the client chat. The owner's reply can update
the current situation and propose confirmed memory; it is never sent to the
client automatically.

## Invariants

- Suggest-only: never reply automatically to a monitored/client chat.
- One Telegram chat maps to one independent Codex thread, wiki, history, and
  `chat_state`.
- Wiki is read-only at runtime.
- Bot-authored messages and commands do not invoke Codex.
- Messages from configured internal LeadRecord participants are captured as
  local context and do not create a recommendation on their own.
- Cross-chat context is limited to explicitly confirmed project/global memory;
  raw client-chat context remains isolated.
- A message is not marked processed until episode logic finishes. A model
  failure leaves the inbox row pending.
- Tokens and credentials never appear unredacted in logs or tracked files.
- Owner-group conversation invokes the agent only on reply-to-bot or an
  explicit mention/tag; ordinary owner-group talk is ignored.
- `AgentProvider` is the only application-to-agent dependency; Telegram code
  must not depend on Codex SDK details.
