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
  -> «Общий контекст: …» in the owner chat: confirmable global memory,
     without a client chat or a reply to a recommendation
  -> reply to a “which chat?” clarification or to the last assistant answer:
     continue that query
  -> reply to a new-group card: client brief, then confirm wiki draft
  -> ordinary human conversation is ignored
```

Each monitored chat has two isolated persistent Codex threads: the client
thread handles client-message recommendations, while the owner-query thread
continues the team's internal questions about that chat. Both receive a fresh
context pack on every turn; neither thread is a durable source of truth.

If the bot becomes admin in an unknown group, or a message arrives from a
group where it is already admin but there is no `chats/*/config.yaml`, the
owner gets a card. Client messages are stored as `held` and are not sent to
Codex until the owner confirms the wiki draft. Telegram does not list the
bot's chats, so a silent already-admin group is discovered on the first
visible message or membership update.

Telegram updates accumulated while the process was offline are ingested first,
then processed as catch-up episodes. `post_init` does not wait for catch-up:
polling starts, backlog is stored, live analysis stays off until a short idle
window after the last ingested update, then catch-up runs. Only the final
episode may notify the owner. `MESSAGE_BATCH_SECONDS` (20 by default) is the
live debounce. `CATCHUP_IDLE_SECONDS` is the startup quiet window.
`CATCHUP_EPISODE_SIZE` splits a large backlog. Different chats never share an
episode.

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
- `agentbridge/knowledge.py`: compact shared knowledge pack loader (`core.md`
  plus an on-demand file index).
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
3. `knowledge/<pack>/core.md` is compact company knowledge. A chat receives it
   when `knowledge_pack` is set; the default for loaded chats is
   `leadgenbureau`. Use `knowledge_pack: none` to opt out. Only the core and a
   file index go into the prompt; extra documents stay on disk for on-demand
   reads. Chat wiki and confirmed chat memory override the shared pack.
4. `chat_state` is a compact working snapshot (facts, decisions, commitments,
   open questions, next step). It is updated after a successful episode.
5. Confirmed memory entries have `fact` / `decision` / `commitment` /
   `preference` / `open_question` / `rule` / `assumption` / `experience` kinds
   and an explicit `chat`, `project`, or `global` scope.
6. Confirmed learning rules and recent confirmed experience are added to the
   context pack when relevant.

Before a model turn the application builds a compact context pack: wiki, shared
core if attached, current `chat_state`, recent history, the current episode,
confirmed memory, rules, and recent experience. Do not send the whole archive
or the whole knowledge pack.

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
- One Telegram chat maps to independent client and owner-query Codex threads,
  plus one wiki, history, and `chat_state`. Each thread is continuity only and
  is stored with `prompt_version`. If `AGENT_PROMPT_VERSION` in
  `agents/codex.py` changes, the next client episode or owner query starts its
  respective new thread instead of resuming the old one. Critique uses a
  separate ephemeral Codex thread and never overwrites `codex_thread_id`.
- Wiki is read-only at runtime, except creating a new `wiki.md` after confirmed
  onboarding.
- Bot-authored messages and commands do not invoke Codex.
- Messages from configured internal LeadRecord participants are captured as
  local context and do not create a recommendation on their own.
- Cross-chat context is limited to explicitly confirmed project/global memory
  and the attached shared knowledge pack; raw client-chat context remains
  isolated. Shared cases are internal and must not be retold to another client.
- A message is not marked processed until episode logic finishes. A model
  failure leaves the inbox row pending.
- Tokens and credentials never appear unredacted in logs or tracked files.
- Owner-group conversation invokes the agent on reply-to-bot, an explicit
  mention/tag, or a global memory prefix (`Общий контекст:`). Ordinary
  owner-group talk is ignored.
- `AgentProvider` is the only application-to-agent dependency; Telegram code
  must not depend on Codex SDK details.
