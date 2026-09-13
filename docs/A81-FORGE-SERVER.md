# A81 — Forge Server: Standalone Task Backend

A81 adds a standalone **Forge Server**: a persistent backend that receives
Forge tasks over an authenticated HTTP API, queues them, executes them in
background worker threads, stores every state transition durably (SQLite),
and lets Forge Desktop (or any HTTP client) disconnect and reconnect later
without losing anything — active tasks keep running, progress/events/logs/
results/approvals are recovered from durable storage.

```text
CLIENT (Forge Desktop / CLI / any HTTP client)
 ↓  HTTPS/HTTP, Bearer token
AUTHENTICATION (API keys fsk_…, sessions fss_…, bootstrap token, lockout)
 ↓
API GATEWAY (FastAPI, /api/v1, closed operation table, rate limits,
 │           structured errors, 1 MiB body cap, no docs/openapi)
 ↓
TASK QUEUE (priority + FIFO, per-project concurrency, leases, retry
 │          backoff, crash recovery)
 ↓
SUPERVISOR (existing A32 transaction: plan → model → code → test/debug →
 │          review → security → benchmark → acceptance → checkpoint →
 │          exact-file git commit)
 ↓
AGENTS → MODEL FABRIC (A46) → TOOL RUNTIME → CHANGE APPLIER
 ↓
A33 PERMISSION/POLICY PLATFORM (unchanged authority: modes, profiles,
 │          approvals, tokens, audit)
 ↓
VERIFICATION → RESULT STORE (SQLite: tasks, events, logs, results,
               approvals, notifications, sessions, projects)
```

The API is a **task backend, never a remote shell**: there is no route,
field, or operation that accepts a command, script, argv, or code payload
for direct execution. Task requirements are *data* for the Supervisor
pipeline; every disk effect flows through the policy-gated Supervisor
transaction.

## What A81 adds

| Area | Location | Notes |
|---|---|---|
| Server package | `forge/server/` | 20 modules: api, auth, authorization, tasks, queue, scheduler, workers, sessions, events, logs, storage, projects, health, notifications, approvals, executor, reconnect, models, errors, server |
| CLI | `forge server`, `forge server status`, `forge server health` | Bootstrap-token generation, stdlib-urllib clients (no extra deps) |
| Storage | SQLite (`.forge/server/server.db` by default) | Single-file WAL database behind one lock-serialized connection, Python 3.8/Windows safe |
| Tests | `tests/test_server_*.py`, `tests/helpers_server.py` | Unit + integration: persistence, queue recovery, worker failure, reconnection, cancellation, policy enforcement, restart recovery, HTTP E2E with the real Supervisor, CLI |

Nothing in A32/A33/A34 changed authority: the Supervisor, permission
platform, approval store, and verification gates are reused as-is.

## Task lifecycle

Statuses (lowercase on the wire):

```text
created → queued → started → running ⇄ paused
                        │        │
                        │        ├→ waiting_for_approval → running
                        │        ├→ completed
                        │        ├→ failed → queued (retry, bounded)
                        │        └→ cancelled
                        └→ (crash/restart) re-queued or failed
completed/failed/cancelled → rolled_back (explicit operator rollback)
```

Every task record carries: `task_id`, `project_id`, `requirement`,
`status`, `stage`, `progress`, `priority`, `mode`, `actor`, `version`
(compare-and-swap for concurrent updates), `retry_count`, `max_retries`,
`checkpoint_id`, `cancel_requested`, `pause_requested`, `available_at`,
`result` (JSON), `error`, and five timestamps (`created_at`, `queued_at`,
`started_at`, `finished_at`, `updated_at`).

Transitions are enforced by a closed table (`forge/server/models.py`);
illegal transitions raise `InvalidTransition` (HTTP 409 with the current
state). Terminal states are `completed`, `failed`, `cancelled`,
`rolled_back`.

## Workers, queue, and crash recovery

* **Background workers** (`workers.py`) are daemon threads that lease
  tasks from the queue, run the executor, and record terminal states.
  They keep working when no client is connected — the API and the workers
  share nothing but durable state.
* **Cooperative control**: pause/resume/cancel flow through
  `SupervisorControl` (`run_control.py`); the Supervisor checkpoints at
  stage boundaries, so cancellation stops a run between stages and rolls
  back its candidate files.
* **Failure handling**: an executor *exception* retries with bounded
  backoff (`retry_count` < `max_retries`, default 2) and then fails; a
  Supervisor *rejection* (tests failed, review/security veto) fails
  immediately without retry — a rejected change set is never retried
  blindly, and the Supervisor has already restored the worktree.
* **Restart recovery** (`server.start()`): tasks interrupted mid-run
  (`started`/`running`/`waiting_for_approval`) are re-queued with
  `retry_count + 1` (or failed honestly when the budget is exhausted);
  stale leases are cleared; **pending approvals are expired at the boot
  boundary** — an approval filed against a dead process can never mint a
  token, so the re-queued task asks again if it still needs a decision.
  A zombie worker from a previous boot is fenced by lease ownership
  ("Lease lost") and its outcome is discarded.

## Authentication & authorization

* **Middleware-first**: an authentication middleware runs *before* body
  parsing on every `/api/v1` route (only `/api/v1/ping` is anonymous), so
  unauthenticated requests can never probe schema validation.
* **Credentials**: bootstrap token (printed once at startup, stored next
  to the DB with `0600` where POSIX allows), API keys (`fsk_…`, stored
  sha256-hashed, roles `admin`/`operator`/`viewer`), and sessions
  (`fss_…`, TTL 12 h, persisted — they survive restarts). Five failed
  attempts per credential per 300 s trigger lockout (429).
* **Scopes**: a closed scope set (`tasks:read/write/control/rollback`,
  `approvals:read/decide`, `projects:*`, `events/logs/results:read`,
  `health/status:read`, `notifications:*`, `sessions/keys:write`,
  `recovery:read`) mapped from roles; every route declares its operation
  in the closed `API_OPERATIONS` table and missing scopes fail closed
  403.
* **A33 integration** (`authorization.py`): task admission is evaluated
  against the server's A33 permission profile with a filesystem probe —
  `safe`/`locked` profiles (or a custom DENY policy) refuse tasks at the
  door, audited. A task's requested mode is *clamped down* to the server
  profile, never up. Execution then runs under the unchanged A33
  platform: the change-set and commit gates stop for operator approval,
  and approval tokens are minted only by the A33 store.
* **Approvals** (`approvals.py`): durable rows + the in-memory A33 store.
  Deciding `approved` records the operator; the *waiting worker* mints a
  scoped, TTL-bounded, use-count-bounded A33 token bound to the change-set
  fingerprint. Denials, expiry, and timeouts fail closed. An agent can
  never approve its own request; a second, differing decision is a 409;
  an idempotent repeat by the same decider returns `duplicate: true`.

## Persistent events, logs, notifications

* **Events** (`events.py`): every meaningful transition emits a durable,
  per-task monotonically numbered event (`task.created`, `task.queued`,
  `task.started`, `checkpoint.created`, `run.started`, `agent.selected`,
  `model.selected`, `change.proposed`, `permission.checked`,
  `approval.required/approved/denied/expired`, `changes.applied`,
  `tests.executed`, `review.completed`, `security.completed`,
  `acceptance.completed`, `git.commit`, `task.completed/failed/cancelled`,
  …). Clients long-poll `GET /api/v1/tasks/{id}/events?after=<seq>&wait=<s>`
  (≤ 25 s) — exact replay from any cursor, durable across restarts, no
  websocket stack (Python 3.8 / Windows safe). Event payloads are
  redacted (A32 report redaction) and capped at 64 KB.
* **Logs** (`logs.py`): rolling per-task log (2 000 kept), id-cursor
  paging.
* **Notifications** (`notifications.py`): durable inbox per project
  (task completed/failed/cancelled, approvals requested/decided), with
  read/read-all.

## Reconnection (`GET /api/v1/recovery`)

One call rebuilds a disconnected client:

```json
{
  "server": {"boot_id": "…", "version": "…", "protocol_version": 1},
  "projects": […],
  "active_tasks": [{"task_id": "…", "status": "running", "stage": "testing",
                    "progress": 0.6, "checkpoint_id": "…", "retry_count": 0}],
  "finished_tasks": […],
  "events": {"<task_id>": [ …events after the client's cursor… ]},
  "logs": {"<task_id>": [ …tail… ]},
  "pending_approvals": […],
  "notifications": […],
  "cursors": {"events": {"<task_id>": 123}, "logs": {"<task_id>": 456}}
}
```

Query params: `after=<event seq>`, `since=<unix ts>`, `project_id=`. The
bundle is bounded (200 events / 100 log lines / 50 results per call);
clients page with the returned cursors. Pending approvals survive
disconnects (but not server restarts — see boot-boundary expiry).

## API surface (all under `/api/v1`)

| Route | Operation → scope |
|---|---|
| `GET /ping` | anonymous liveness |
| `GET /auth/whoami` | principal echo |
| `POST/GET /auth/sessions` | `sessions:write` / list own |
| `POST/GET /auth/keys` | `keys:write` (admin) / list |
| `POST/GET /projects` | `projects:write` / `projects:read` |
| `POST /tasks` · `GET /tasks` · `GET /tasks/{id}` | `tasks:write` / `tasks:read` |
| `POST /tasks/{id}/pause|resume|cancel|retry|rollback` | `tasks:control` (`rollback`: `tasks:rollback`) |
| `GET /tasks/{id}/logs|result|events` | `logs/results/events:read` |
| `GET /approvals` · `GET /tasks/{id}/approvals` · `POST /approvals/{id}/decide` | `approvals:read` / `approvals:decide` |
| `GET /notifications` · `POST /notifications/{id}/read` · `POST /notifications/read-all` | `notifications:*` |
| `GET /recovery` | `recovery:read` |
| `GET /health` · `GET /status` · `GET /policy` | `health/status:read` |

Guarantees: structured errors `{"error": {"code", "message",
"request_id"}}` (no tracebacks), `X-Request-ID` on every response,
token-bucket rate limiting (120 req/min, burst 30), 1 MiB body limit,
`extra="forbid"` schemas with bounded fields, execution-shaped field
names rejected outright, and `/docs`, `/redoc`, `/openapi.json` disabled.

**Health** (`health.py`) reports `ok`/`degraded`/`down` with per-component
checks: `database`, `workers`, `scheduler`, `queue`, `disk`, `approvals`.

## CLI

```bash
forge server                     # start (defaults: 127.0.0.1:8300)
forge server start --host 127.0.0.1 --port 8300 \
    --db .forge/server/server.db --project demo=/path/to/repo \
    --workers 4 --profile assisted [--token TOKEN]
forge server status              # live status of a running server
forge server health              # component health report
forge server status --json       # machine-readable
```

* With no `--token`, a bootstrap token is generated, printed once, and
  written to `<db-dir>/token` (`0600` on POSIX). `forge server
  status/health` resolve credentials from `--token`, then
  `FORGE_SERVER_TOKEN`, then that token file.
* `status`/`health` use only `urllib` from the standard library — they
  work even where the FastAPI stack is not installed.
* Environment overrides: `FORGE_SERVER_DB`, `_HOST`, `_PORT`, `_TOKEN`,
  `_PROFILE`, `_MAX_WORKERS`, `_MAX_TASKS_PER_PROJECT`, `_MAX_RETRIES`,
  `_RETRY_BACKOFF`, `_APPROVAL_TIMEOUT`, `_SESSION_TTL`, `_AUTH_MODE`.

## Configuration defaults (`ServerConfig`)

| Field | Default |
|---|---|
| `db_path` | `.forge/server/server.db` |
| `host` / `port` | `127.0.0.1` / `8300` |
| `profile` | `assisted` |
| `max_workers` | 4 |
| `max_tasks_per_project` | 1 |
| `default_max_retries` | 2 |
| `retry_backoff_seconds` | 2.0 |
| `approval_timeout` | 600 s |
| `approval_token_ttl` | 300 s |
| `session_ttl` | 12 h |
| `max_events_per_task` / `max_logs_per_task` | 5000 / 2000 |
| `checkpoint_retention` | 10 |

## Integration points

* **Supervisor** (`forge/core/supervisor.py`) — the real guarded
  transaction is the default executor (`SupervisorExecutor`); its outcome
  (accepted files, report, gates, timings, token usage, rollback state)
  is stored verbatim as the task result, with post-run Git status and a
  Verification security-gate summary attached.
* **Model Fabric** (A46) — the server owns one fabric instance shared by
  all workers.
* **Runtime / Native AI engine** — untouched; writes flow through the
  permissioned runtime inside the Supervisor.
* **Memory** — terminal tasks record task memory exactly as the CLI
  pipeline does (`record_task_memory`).
* **Checkpoints** — created per run (`checkpoint.created` event,
  `checkpoint_id` on the task); explicit operator rollback
  (`POST /tasks/{id}/rollback`) restores the checkpoint and moves the
  task to `rolled_back`.
* **Git** — commits remain exact-file, message-prefixed (`forge: …`), and
  approval-gated even in `autonomous` mode.
* **Verification** — review/security/benchmark/acceptance gates run
  inside the transaction; the post-run security summary is part of the
  durable result.

## Python 3.8 / Windows compatibility

`from __future__ import annotations` everywhere; `typing` generics
(no PEP 585 runtime use); `ThreadPoolExecutor` without `cancel_futures`;
no `asyncio.run` dependencies in the core (workers are threads); stdlib
`sqlite3` via one shared connection (`check_same_thread=False`) with every
access serialized by a lock; long-poll events instead of websockets;
`urllib` CLI clients;
path handling via `pathlib` with repo-relative A33 scopes. The repository
compat suite (`tests/test_python38_compat.py`) covers the new modules.

## Tests

`tests/test_server_*.py` + `tests/helpers_server.py`:

| Suite | Coverage |
|---|---|
| `test_server_tasks.py` | lifecycle table, CAS versioning, timestamps, fields, terminal rules |
| `test_server_queue.py` | priority/FIFO, leases, per-project concurrency, backoff, recovery |
| `test_server_workers.py` | execution, retries, failure honesty, memory recording |
| `test_server_cancellation.py` | queued/running/approval-wait cancellation, pause/resume, rollback of candidates |
| `test_server_events.py` | durable sequencing, cursors, long-poll, redaction, pruning |
| `test_server_api.py` | auth matrix, scopes, CRUD, 409 CAS, validation, structured errors |
| `test_server_policy.py` | profile admission, mode clamping, custom DENY, real A33 tokens, self-approval ban, no-remote-shell guarantees |
| `test_server_reconnect.py` | disconnected operation, recovery bundle, exact cursor replay, pending-approval recovery |
| `test_server_restart.py` | queued survival, interrupted re-queue, zombie fencing, budget exhaustion, session/event survival, boot-boundary approval expiry |
| `test_server_e2e.py` | full Supervisor pipeline over HTTP (CSV export: approvals → apply → tests → review → security → acceptance → git commit), rejected change-set rollback, autonomous profile, two independent projects |
| `test_server_cli.py` | helpers, argparse wiring, token generation, status/health matrix, live uvicorn roundtrip |

Run everything with `python -m pytest -q` (offline, deterministic; the
scripted model provider keeps the E2E independent of any live model).
