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
No Python dependencies were added. The frontend build uses the committed npm lockfile.

Invite-only accounts: `evgen` (owner), `dmitry` (Rick), `sergey` (Sally). No public
registration. Owner creates single-use invitations, selects explicit chat/topic
IDs, and can revoke access; reinviting a revoked account resets its password and
scopes. Owner setup: `python -m agentbridge.web.admin --send-owner`; this is an
explicit operator action, never an automatic startup message. It sends the link
only to the configured Rick OWNER_CHAT_ID and keeps a mode-0600 recovery file.
Tokens are in URL fragments and POST bodies, never request logs. Never print that
file, cookies or invitation tokens into deployment logs.

`runtime/web/access.sqlite3` stores scrypt password hashes, hashed session/invite
tokens, explicit grants, throttle counters and durable web requests/results. It
does not become agent memory. Sessions have distinct product cookies, paths and
server-side product checks; Secure/HttpOnly/SameSite=Strict, seven-day expiry,
CSRF tokens and strict Origin checks for POST, including login/enrollment.
Login throttles both IP and account. Invite expiry is 24 hours.
Read paths use SQLite mode=ro/query_only and filter each context before reads.
API output never includes auth secrets, Codex thread IDs, env or file paths.

Commands use a per-user/product idempotency key with payload conflict detection,
one in-flight request per account, a persistent claim and another ACL check in the
worker. Interrupted running requests become `uncertain`, not silently retried.
The UI retains its key after a failed send. Requests/results are available only
to their author and currently granted context. Job records are transport history,
not a second business-fact store.

Rick owner questions reuse the existing owner query method/thread/context and
owner memory policy. A per-chat owner-query lock serializes web and Telegram turns.
Member questions use the existing provider/SDK in ephemeral tool-free read-only,
deny-all turns with only that chat's context, no owner file or owner thread.
There are no web commands for /dev, code, restart, wiki or memory approval.

Sally web text is persisted through `SalesStore.ingest`, with `source=web_text`
and negative synthetic update/message IDs in a reserved range. It enters the
existing topic's batching/lock/checkpoint/facts/recommendations/outbox cycle.
Normal Telegram delivery puts the answer in that topic; no client chat is sent.
The pending web job observes the persisted event/result, rather than starting a
parallel turn. Existing pending fact reviews block new text jobs for that topic.

The UI exposes real recommendations, sources, available history (latest 100
messages), current facts, commitments and reminder delivery fields. No fictional
KPIs, dates, demo rows, success or online badges. Existing Sally relaxed fact
validation is preserved. Pending fact reviews and owner approvals stay in their
Telegram workflow; the UI links to the corresponding topic/owner chat. Voice,
files, reminders and memory approvals continue through Telegram. This avoids
pretending a browser actor has a Telegram membership/message callback identity.

The service worker caches only hashed build assets, never HTML/API/client history.
No client data is written to localStorage. Manifests have different id/start_url,
scope and original icons. Real Android installation/keyboard QA is still manual.

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
services, and diagnose. Business schemas are unchanged by the web adapter; do not
restore older business databases and lose newer Telegram events. Keep the web DB
for request/account recovery. Static releases and backups are mode-protected.

Temporary test checkouts and local basetemp files are removed after validation.
Production history, current releases, backups and the supplied concept kit remain.

Validation before rollout (2026-10-16): 784 Python tests passed on Linux in an
isolated VPS checkout; frontend TypeScript/build, ESLint and five Vitest tests
passed. Security tests use fake providers and temporary SQLite stores. Android
hardware testing remains with the owner. The original design-phase screenshots
and intermediate measurement artifacts were removed from main after selection.
