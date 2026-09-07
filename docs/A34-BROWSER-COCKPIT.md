# A34 — Browser Cockpit + Secure Control Plane

A34 turns Forge into a browser-operated autonomous AI platform. The browser
is a cockpit: it observes and commands, but it is **never trusted**. Every
authorization decision is enforced server-side by the existing A33
policy/permission system; the browser talks only to a versioned API, and the
API talks only to the control plane, which drives the existing A32
autonomous pipeline.

```text
USER
 ↓
BROWSER COCKPIT (forge/cockpit/web: HTML + CSS + vanilla JS, no build)
 ↓  same-origin /api/v1 only
SECURE API (forge/api: FastAPI)
 ↓
AUTHENTICATION / SESSION (project-scoped sessions)
 ↓
CONTROL PLANE (forge/control)
 ↓
A33 POLICY + PERMISSION SYSTEM (unchanged authority)
 ↓
SUPERVISOR → TASK SYSTEM → AGENT PLANNER → MODEL FABRIC → AGENTS
 ↓
TOOL RUNTIME → CHANGESET → VERIFICATION → CHECKPOINT/ROLLBACK → GIT
```

There is deliberately no path from the browser to the filesystem, shell, or
model providers.

## What A34 adds

| Area | Location | Notes |
|---|---|---|
| Control plane | `forge/control/` | Sessions, runs, worker, events, approval adapter, rollback, views, audit |
| Versioned API | `forge/api/` | `/api/v1/*`, SSE stream, errors, auth, rate limits |
| Browser cockpit | `forge/cockpit/web/` | No build step, no dependencies, no secrets |
| Core hooks | `supervisor.py`, `change_applier.py`, `coder.py`, `debugger.py`, `run_control.py` | Backward-compatible, inert without A34 callers |
| Server entry | `forge serve` | Uvicorn, localhost by default |
| Tests | `tests/test_a34_*.py`, `tests/helpers_a34.py` | 63 tests: API, security, E2E |

Framework choice: FastAPI + uvicorn (spec-preferred, small, testable with
httpx). The frontend is dependency-free vanilla JS rather than TypeScript:
with no build toolchain the bundle stays trivially auditable and CI needs
no Node.js. SSE is used for the live stream instead of WebSockets — the
spec explicitly accepts SSE, and it needs no extra dependencies.

## Running locally

```bash
pip install -e ".[dev]"
forge serve --project myproject=/path/to/repo
# open http://127.0.0.1:8000
```

Sign in with an actor name (audit identity), a registered project id, and a
permission profile (`safe` / `assisted` / `autonomous` / `locked`).
`forge serve --host 0.0.0.0` listens publicly; the default `127.0.0.1` is
loopback-only. Authentication is **local-development sessions** (no
passwords); the health endpoint and server banner say so. Do not expose the
dev server to untrusted networks.

Configuration (environment, no secrets in code):

| Variable | Default | Meaning |
|---|---|---|
| `FORGE_DB_PATH` | `.forge/cockpit.db` | Control-plane database |
| `FORGE_ALLOWED_ORIGINS` | (empty) | Comma-separated CORS origins; empty = same-origin only. `*` is rejected |
| `FORGE_SECURE_COOKIES` | `0` | Set `1` behind HTTPS |
| `FORGE_APPROVAL_TIMEOUT` | `600` | Seconds a run waits for one approval |
| `FORGE_MAX_EVENTS` | `5000` | Retained events per task |
| `FORGE_MAX_WORKERS` | `4` | Worker threads (runs per project stay serialized) |
| `FORGE_MAX_SESSIONS` | `200` | Active session cap |
| `FORGE_SESSION_TTL` | `43200` | Session lifetime (seconds) |
| `FORGE_AUTH_MODE` | `local-dev` | Label only; anything but `production` reads `local-dev` |

## API reference (`/api/v1`)

Interactive docs: `/api/docs` (served by the app). All endpoints except
`GET /health` and `POST /sessions` require a session: `Authorization:
Bearer <token>` or the HttpOnly `forge_session` cookie. Cookie-authenticated
mutations must also send `X-Requested-With: forge-cockpit`.

```text
GET    /health                          # public; includes auth_mode label
GET    /health/models                   # model/provider health
POST   /sessions                        # {actor, project_id, profile}
GET    /sessions/me
DELETE /sessions/me
GET    /dashboard                       # counts, approvals, models, workers

GET    /projects
GET    /projects/{id}                   # session project only
GET    /projects/{id}/git               # branch, HEAD, worktree, commits
GET    /projects/{id}/git/diff?staged=  # bounded diff (200 KB cap)
POST   /recover                         # mark interrupted runs failed

GET    /tasks?status=&limit=&offset=
POST   /tasks                           # {requirement, mode?}
GET    /tasks/{id}
POST   /tasks/{id}/pause|resume|cancel  # {expected_version?}
POST   /tasks/{id}/retry
GET    /tasks/{id}/events?after=&limit= # cursor replay
GET    /tasks/{id}/events/stream?after= # SSE live stream
GET    /tasks/{id}/report
GET    /tasks/{id}/logs
GET    /tasks/{id}/verification
GET    /tasks/{id}/checkpoints
POST   /tasks/{id}/rollback             # {checkpoint_id?, expected_version?}

GET    /runs, GET /runs/{id}            # read aliases over tasks

GET    /approvals
GET    /approvals/{id}
POST   /approvals/{id}/approve|deny
GET    /permission-requests             # alias
POST   /permission-requests/{id}/approve|deny

GET    /models
GET    /models/health
GET    /providers
GET    /permissions                     # profile + effective + pending

POST   /commands                        # finite vocabulary (see below)
POST   /interpret                       # NL -> command, never executes
POST   /voice/interpret                 # voice intent + A33 check, advisory
GET    /desktop/capabilities            # foundation-only listing
POST   /desktop/check                   # permission preview, never executes
```

### Safe command vocabulary

`START_TASK PAUSE_TASK RESUME_TASK CANCEL_TASK RETRY_TASK APPROVE DENY
ROLLBACK` — a closed enum. Unknown commands are `INVALID_REQUEST`;
natural-language/voice text is translated deterministically (no model in
the loop) and unknown or shell-like input becomes `REQUIRE_CLARIFICATION`
or `DENY`. `/interpret` and `/voice/interpret` never execute.

### Errors

```json
{"error": {"code": "APPROVAL_REQUIRED", "message": "...",
           "request_id": "...", "details": {"approval_id": "..."}}}
```

Stable codes: `AUTH_REQUIRED FORBIDDEN PROJECT_NOT_FOUND TASK_NOT_FOUND
APPROVAL_NOT_FOUND INVALID_REQUEST APPROVAL_REQUIRED APPROVAL_CONFLICT
APPROVAL_EXPIRED POLICY_DENIED TASK_CONFLICT TASK_NOT_RUNNING
ROLLBACK_FAILED MODEL_UNAVAILABLE CSRF_REQUIRED RATE_LIMITED NOT_FOUND
INTERNAL_ERROR`. No tracebacks, no secrets, no internal paths. Every
response carries `X-Request-ID`.

### Event catalog

Control-plane events: `task.created task.queued task.started task.paused
task.resumed task.cancel_requested task.cancelled task.completed task.failed
stage.started run.started agent.selected model.selected change.proposed
permission.checked changes.applied tests.executed tests.failed
repair.attempted review.completed security.completed benchmark.completed
acceptance.completed git.commit checkpoint.created rollback.requested
rollback.completed approval.required approval.approved approval.denied
approval.expired`.

```json
{"seq": 12, "event_id": "...", "task_id": "t-...",
 "timestamp": 1788773803.3, "type": "stage.started",
 "data": {"stage": "testing", "raw": "TEST"}}
```

Sequences are per-task monotonic; `after=<seq>` replays exactly what was
missed, and the SSE stream honors both `?after=` and `Last-Event-ID`.
Payloads are secret-redacted.

## Security model

- **Untrusted browser.** Sessions authenticate; project scope, task scope,
  permission, policy, approval, and command validation all happen
  server-side. Disabled buttons are convenience, not security (tested:
  hidden actions are rejected by the API).
- **One permission system.** A34 adds no policy logic. The control plane
  files A33 approval requests, waits, and redeems A33 tokens on the normal
  enforcement path; rollback authorizes every file through the existing
  `PolicyGate`. `DENY` is never escalated to an operator.
- **Scoped, task-bound, time-bounded, non-replayable approvals.** Tokens
  are minted with exact file counts and proposal fingerprints; decisions
  are single-transition (same-approver retries are idempotent, conflicts
  are 409); expiry fails closed; agents cannot self-approve (tested).
- **No bypass fields.** Client-controlled `approved/bypass/admin` keys are
  not part of any schema and are ignored (tested: runs still wait for
  approval).
- **Isolation.** Sessions bind one project; cross-project ids return 404
  (existence does not leak); approvals resolve to runs before any decision.
- **Browser/transport hardening.** Same-origin default CORS (`*` refused at
  startup), CSRF header for cookie mutations, HttpOnly SameSite=Lax
  cookies, bounded bodies (1 MB), bounded pagination, per-IP rate limits,
  CSP + nosniff headers, no-store on the app shell.
- **Audit.** Every sensitive action (submit, pause, resume, cancel, retry,
  approve, deny, rollback, recovery, session create/revoke, desktop
  preview) is recorded in the A33 audit log with a JSONL sink.
- **Cooperative control.** Pause/cancel take effect at stage boundaries;
  cancellation rolls back exactly like a failure (`CANCELLED`, never a
  half-applied tree). Pause during an approval wait resolves the wait
  first, then pauses — this is documented, honest behavior.

## Background execution & persistence

`POST /tasks` persists a task (existing `PersistentTaskQueue` + `TaskStore`)
and returns immediately; a dispatcher + worker pool runs the existing
`Supervisor.run` with live-event, pause/cancel, and interactive-approval
hooks. Closing the browser never stops a run. Sessions, runs, events, and
checkpoint metadata live in SQLite and survive refresh and API restart;
in-flight runs are marked failed (never silently resumed) after a restart.
Approval requests/tokens are in-memory A33 state: pending approvals do not
survive a restart (fail closed), which the UI surfaces as expiry.

## Cockpit screens

Dashboard (real counts, model/worker health, recent activity), Tasks (submit
+ list), Task detail (pipeline timeline from real `stage.started` events,
details, verification gates, checkpoints, report, live event feed), Projects
(registry + current project + git), Models (fabric models, providers,
routing policy, recent routing), Permissions (effective profile from the
real `PermissionManager`, full WHAT/WHY approval cards, policy rules), Git
(branch, worktree, changed files, staged/unstaged diffs).

## Known limitations (honest)

- Local-dev auth only: no passwords, no SSO; sessions are bearer tokens.
  Production needs real authentication in front of the API.
- SSE, not WebSockets (explicitly acceptable per spec).
- Pending A33 approvals do not survive an API restart (fail closed).
- Browser rollback restores worktree files only; the task's git commit
  stays in history (stated in the UI/docs).
- Pause/cancel are cooperative at stage boundaries (a long model call
  finishes first).
- Voice transcription, desktop control, and browser automation are
  foundations/abstractions only — nothing executes.
- `CUSTOM` profiles are rejected; `safe/assisted/autonomous/locked` only.
- No framing controls by default (documented; add at the edge for prod).
