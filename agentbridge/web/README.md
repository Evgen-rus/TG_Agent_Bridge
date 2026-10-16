# Rick A / Sally A: production web adapters

Two installable identities at `/rick/` and `/sally/`, hosted by the existing Rick
process on loopback `127.0.0.1:18082`, behind HTTPS nginx. No new Telegram poller,
framework, service, ORM, broker or model key. The Sally worker runs inside the
existing Sally process. Both are opt-in using `AGENTBRIDGE_WEB_ENABLED=true`.

`AGENTBRIDGE_WEB_ORIGIN` must be the exact HTTPS origin, without a trailing slash.
`AGENTBRIDGE_WEB_PORT` defaults to 18082. Build `mobile-concepts` before startup.
`AGENTBRIDGE_WEB_SALES_ENV_FILE` points to the existing separate Sally config
(default `runtime/sales_buddy/.env`). Rick reads its non-secret database location
and group ID for authorized views; bot credentials are not copied into Rick's env.
The process reads only `mobile-concepts/dist` and the two original application icons.
PDF text extraction uses pypdf; Web image validation uses Pillow, both pinned in requirements.txt. The frontend build uses the committed npm lockfile.

Invite-only accounts: `evgen` (owner), `dmitry` (Rick), `sergey` (Sally). No public
registration. Owner creates single-use invitations and can revoke access. Dmitry can access
all current and future monitored Rick chats; Sergey can access all current and
future topics in the configured Sally group. Existing accounts and unused
invitations receive this product-wide access without re-enrollment; product
isolation and disabled-account checks remain enforced. Reinviting a revoked
account resets its password. Owner setup: `python -m agentbridge.web.admin --send-owner`; this is an
explicit operator action, never an automatic startup message. It sends the link
only to the configured Rick OWNER_CHAT_ID and keeps a mode-0600 recovery file.
Tokens are in URL fragments and POST bodies, never request logs. Never print that
file, cookies or invitation tokens into deployment logs.

`runtime/web/access.sqlite3` stores scrypt password hashes, hashed session/invite
tokens, product roles, throttle counters and durable web requests/results. It
does not become agent memory. Sessions have distinct product cookies, paths and
server-side product checks; Secure/HttpOnly/SameSite=Strict, seven-day expiry,
CSRF tokens and strict Origin checks for POST, including login/enrollment.
Login throttles both IP and account. Invite expiry is 24 hours.
Read paths use SQLite mode=ro/query_only and validate each context against the live registry or configured Sally group before reads.
API output never includes auth secrets, Codex thread IDs, env or file paths.

Commands retain per-user/product idempotency keys and payload conflict detection.
Sally's Web worker persists each outgoing Telegram part before sending, saves its
acknowledged message ID, then ingests through the existing SalesStore, batching,
media workers, checkpoints and topic outbox. Real authors remain internal Web
users; the bot labels sends “Имя · через приложение”. SQLite is the common
business history. No duplicate Bot API echo is processed. Interrupted acknowledged
sends can resume; ambiguous sends and Rick model turns become `uncertain` and
are not automatically replayed. The UI distinguishes saved, pending and uncertain.
Sally's Web-triggered answers use the same ambiguity guard. Check the Telegram
topic before any manual repeat. Bot API has no idempotency token for exactly-once
sends; an uncertain result cannot safely be resolved by blindly resending.

The Sally conversation reads the original events plus topic outbox for everyone,
including author, reply IDs, edit versions, processing status and attachments.
General is a separate context (canonical topic 0) in the configured forum, with
an independent persistent internal-assistant thread. Messages without a topic
flag and topic 1 map to General; other flagged topics remain isolated. Sally
responds there only to name/tag or replies to her messages. General has no sales
card or automated deal commitment tracking. Only saved history is available;
Bot API cannot fetch arbitrary old messages.

Rick questions never send to client chats. Owner questions reuse the original
owner provider/context policy. Dmitry uses the existing tool-free, read-only,
deny-all provider with only the selected work context and authorized internal
conversation. Owner-private turns, owner working memory, /dev, code, restart and
configuration remain inaccessible. Member work is visible to owner; owner-private
queries are not shared with members. Client recommendations/events remain visible
in their original screens, including available Telegram attachments.

Both apps support 8 attachments per request, 20 MiB per file, multiple images,
TXT/MD/PDF/DOCX and browser voice recording with preview/cancel. `api/upload` is
a separate binary endpoint with Origin, CSRF, authentication, context ACL,
rate/concurrency limits and a 200 MiB local-source quota per account. Media bytes
are private and are served only by an authorized `api/media` request, never by
static paths. A Telegram-backed cache may be evicted after seven days and fetched
again; unsent and Rick source uploads are retained within quota. Expired unused
uploads and abandoned partial files are removed. Images are magic-byte and
Pillow-verified, limited to 40 million pixels. Audio containers use existing STT
settings; Android WebM is kept as audio/WebM, sent to Telegram as a document.
Sally uses the existing durable AudioWorker/ImageWorker; Rick uses its existing
provider and shared transcriber. No extra model key or polling process is added.

Shared `agentbridge/documents.py` extracts documents for both Web and Telegram in
a bounded subprocess: 25 seconds, Linux 512 MiB address space / 20 CPU seconds,
PDF up to 100 pages, at most 120000 text characters, bounded DOCX ZIP/XML.
TXT/MD must be UTF-8 and at most 2 MiB. PDF layout and DOCX table cells are
preserved as text where possible. Encrypted, damaged or oversized files fail
explicitly. Scanned PDFs ask for page images; automatic whole-PDF OCR is not added.

Drafts survive failed uploads/sends and context switches while logged in. Successful
uploads are reused on a retry; the original command key is retained. Approval,
reminder changes and memory confirmation continue through Telegram, without
impersonating Web actors as Telegram callback users.

The service worker caches only hashed build assets, never HTML/API/client history.
No client data is written to localStorage. Manifests have different id/start_url,
scope and original icons. Real Android installation/keyboard QA is still manual.

The context list refreshes every 15 seconds and when the window regains focus.
New empty Telegram topics appear once Sally persists them in her deals registry.

## Deployment and rollback

1. Run frontend build/lint/tests and Python fake/security/regression tests.
2. Commit and push `main`; package that revision's `dist` separately.
3. On VPS, record revision/status; back up both SQLite files with SQLite backup,
   verify backup integrity and restore to a disposable copy before updating.
4. `git pull --ff-only origin main`, install the checked frontend artifact under
   ignored runtime release storage, link `mobile-concepts/dist` to that release.
5. Add non-secret opt-in settings. Include `deploy/nginx-agentbridge-locations.conf`
   only in the existing HTTPS server, leaving its default location/services intact.
   Validate nginx before reload; restart only Rick and Sally.
6. Check diagnostics, both bot services, auth/ACL failures, manifests and PWA shell.
   Send owner bootstrap only after these checks succeed.

For rollback: disable web opt-in, restore the prior nginx server file, select the
recorded Git revision (preserving any newer owner-context work), restart these two
services, and diagnose. Schema additions preserve existing rows and IDs; do not
restore older business databases and lose newer Telegram events. Keep the web DB
for request/account recovery. Static releases and backups are mode-protected.

Temporary test checkouts and local basetemp files are removed after validation.
Production history, current releases, backups and the supplied concept kit remain.

Validation before rollout (2026-10-16): 802 Python tests passed on Linux in an
isolated VPS checkout; frontend TypeScript/build, ESLint and eight Vitest tests
passed. Security tests use fake providers and temporary SQLite stores. Android
hardware testing remains with the owner. The original design-phase screenshots
and intermediate measurement artifacts were removed from main after selection.
