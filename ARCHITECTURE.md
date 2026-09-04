# AgentBridge architecture

This document is a compact orientation map for future Codex work. The current
code and tests are authoritative when they differ from this summary.

## Flow

```text
Telegram long polling (drop_pending_updates=False)
  -> hard-deadline getUpdates transport + monotonic polling heartbeat
  -> watchdog restarts a stalled updater, or fails the process if recovery fails
  -> persist each relevant update in SQLite (durable inbox/history)
  -> transcribe voice notes in the background (TRANSCRIPTION_MODEL via
     AgentBridge.transcribe, transcript stored by update_id in SQLite)
  -> download current-episode photos/PDFs via saved file_id into runtime/media
  -> live debounce per chat, or catch-up after restart
  -> application: episode from stored messages, context pack, Rick model turn
  -> AgentProvider (images as LocalImageInput, other files as MentionInput)
  -> CodexProvider: start or resume the chat's Codex thread
  -> optional ephemeral Sepia refactor turn with draft + compact facts only
  -> lightweight fact/commitment flags + exact number/link guard
  -> delete local media copies; keep file_id in SQLite
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
  -> /rules and /undo from the owner-chat command menu only
  -> ordinary human conversation is ignored
```

Each monitored chat has two isolated persistent Codex threads: the client
thread handles client-message recommendations, while the owner-query thread
continues the team's internal questions about that chat. Both receive a fresh
context pack on every turn; neither thread is a durable source of truth. The
two paths use separately configured `CodexProvider` instances: `CODEX_MODEL` /
`CODEX_REASONING_EFFORT` for client work and `OWNER_CODEX_MODEL` /
`OWNER_CODEX_REASONING_EFFORT` for internal Owner queries, feedback analysis,
and onboarding drafts. Regenerating a client recommendation after confirmed
feedback still uses the client provider. When `SEPIA_ENABLED` is true, only a
final `reply` draft is sent to the project-local `sepia-refactor` skill together
with the `client-chat` voice profile. Sepia gets no Telegram history or media.
If its preservation flags fail or an exact number/link changes, the original
Rick draft is kept. Owner queries, observations, and questions are not refactored.

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

Polling liveness is independent of message traffic: every completed or failed
`getUpdates` attempt advances a monotonic heartbeat, including empty responses.
A hard request deadline prevents a single HTTP call from waiting forever. If
the heartbeat becomes stale, the in-process watchdog stops and restarts the
Updater with `drop_pending_updates=False`. If that bounded recovery fails, it
requests application shutdown and `main` exits non-zero so an external
supervisor can restart the whole process. The SQLite last-message time is not a
health signal because all monitored chats may legitimately be quiet.

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
- `agentbridge/media.py`: short-lived local copies of client photos/PDFs
  (deleted after the episode, TTL 1 hour) plus labels for history.
- `agentbridge/transcribe.py`: OpenAI speech-to-text call used for client voice
  notes; the configured `TRANSCRIPTION_MODEL` is the only transcription model.
- `agentbridge/storage/sqlite.py`: durable Telegram history, threads, chat
  state, processed updates, pending/delivered recommendation links, learning
  drafts, versioned rules, memory, owner questions, experience, and Telegram
  `file_id` metadata for client attachments.
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
periodically and on startup. All owner text is split losslessly into numbered
parts when needed to fit Telegram's 4096-character limit (with a conservative
UTF-16 budget including part headers). Confirmation buttons appear only on the
last part. SQLite `owner_delivery_parts` freezes the split and records each
accepted part. Recommendation and owner-query retries, including after restart,
skip acknowledged parts and mark the whole delivery complete only after its
final part. Replies to any part of a completed message are resolved to the final
message ID, preserving recommendation, ASK_OWNER, and query-continuation links.
Other multi-part owner notices use the same reply mapping; only existing durable
delivery queues are retried automatically. This is at-least-once delivery: a crash
between Telegram acceptance and SQLite acknowledgement can duplicate that part.
Telegram BadRequest rejections are logged separately from network/time-out errors,
without logging raw exception text that could contain credentials.

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
- A live Python process is not considered proof of Telegram polling health;
  `getUpdates` heartbeat progress is monitored independently and stale polling
  is recovered without dropping Telegram backlog.
- Client photos, PDFs, and media albums are analysed in that chat's Codex
  thread. Local files are working copies only (deleted after a successful
  episode, and anyway after `MEDIA_TTL_SECONDS`, default 1 hour). SQLite keeps
  Telegram `file_id` so a later turn can re-download. The bot cannot scroll a
  group's history like a user account.
- Voice notes follow the same persist-first path: the row is stored before any
  model work, then transcribed in the background during the batch window (or at
  catch-up after restart). The transcript is stored by `update_id`; Codex sees
  `[голосовое] текст`, never the audio file. API failure or silence becomes a
  placeholder so a batch is never blocked; without `OPENAI_API_KEY` voice rows
  simply stay pending. Owner-chat voice notes are transcribed on the fly and
  then processed as ordinary owner text (feedback, answers, mention queries).
- Tokens and credentials never appear unredacted in logs or tracked files.
- Owner-group conversation invokes the agent on reply-to-bot, an explicit
  mention/tag, a voice transcript beginning with `Рик` or `Агент`, or a global
  memory prefix (`Общий контекст:`). Ordinary owner-group talk is ignored.
  Telegram media downloads use bounded retries. If an owner voice download
  still fails, its error message temporarily retains the original reply target
  so an immediate voice retry continues the intended bot-message chain.
- `AgentProvider` is the only application-to-agent dependency; Telegram code
  must not depend on Codex SDK details.
