# A81 — Forge Desktop ↔ Forge Server link

The Lenovo G560 (or any modest machine) runs **Forge Desktop as a
lightweight client**; a stronger machine runs the **Forge Server** and
performs the heavy engineering work (model inference, the full
pipeline, tests, verification). The client never loads a model and
never runs the engineering pipeline.

```
┌────────────────────────────┐          signed HTTP (A81)          ┌──────────────────────────────┐
│ Forge Desktop (Lenovo G560)│  challenge/handshake + HMAC per     │ Forge Server (workstation)   │
│  forge.client.ForgeClient  │  request; stdlib urllib only        │  cockpit app + ControlPlane  │
│                           │────────────────────────────────────▶│  dispatcher + worker pool    │
│  LOCAL   bounded light ops │   tasks / logs / events /           │  Model Fabric (real models)  │
│  SERVER  remote pipeline   │   approvals / verification          │  SQLite: runs, events, link  │
│  HYBRID  automatic choice  │◀────────────────────────────────────│  authoritative authorization │
└────────────────────────────┘                                     └──────────────────────────────┘
```

## What was implemented

| Requirement | Where |
|---|---|
| 1. Connection settings (server URL, client id, auth state, connection status, reconnect policy, timeout) | `forge/client/config.py` (`ClientConfig`, `ReconnectPolicy`, `LocalPolicy`) + Server tab in the desktop app |
| 2. LOCAL / SERVER / HYBRID modes | `forge/client/config.py` (`mode`), `forge/client/router.py` |
| 3. LOCAL executes locally when allowed | `forge/client/local_exec.py` (closed 3-operation vocabulary, bounded) |
| 4. SERVER sends tasks to the Forge Server | `forge/client/transport.py` → `/api/v1/link/*` → `ControlPlane.submit_task` |
| 5. HYBRID auto-selection (capability, resources, model availability, policy, task size) | `forge/client/router.py` (`decide()` — pure, deterministic, reason recorded) |
| 6. Desktop display: connected/disconnected, active tasks, queue, current stage, model, worker, logs, approval requests, verification result | `forge/desktop_app/app.py` Server tab + `forge/client/client.py` `snapshot()` |
| 7. Automatic reconnect | `forge/client/connection.py` (state machine + exponential backoff + heartbeat) |
| 8. Server continues when the laptop disconnects | Server worker pool is client-independent; `tests/test_link_persistence.py` |
| 9. Reopen restores server task state | `ForgeClient.restore()` / `DesktopBackend.link_connect()`; state lives server-side in SQLite |
| 10. G560 stays lightweight; no large model on the client | The client stack imports no model fabric; LOCAL is bounded read-only ops; model-requiring tasks are never local |
| 11. Security (authenticated, no plaintext credentials, no arbitrary remote execution, server authorization authoritative) | `forge/link/protocol.py`, `forge/server/service.py` (mode clamping, fixed endpoints), tests |
| 12. Deterministic tests | `tests/test_link_*.py`, `tests/test_desktop_app_link.py` |
| 13. Exact client configuration | This document, next section |

## Exact client configuration

### One-time server setup (on the Forge Server machine)

```bash
# 1. Run the Forge Server (cockpit API + link endpoints).
python -m forge.server serve --host 0.0.0.0 --port 8000 \
    --project demo=/srv/forge/demo

# 2. Register the laptop; this prints the ONE-TIME secret.
python -m forge.server add-client g560 --project demo=/srv/forge/demo \
    --name "Lenovo G560" --max-mode assisted
```

`--max-mode` is the **authorization ceiling** for this client
(`safe` < `assisted` < `autonomous`). The server clamps every task's
requested mode to this ceiling: a client can tighten (ask for `safe`)
but never loosen. Copy the printed secret to the laptop once; the
server stores only `SHA-256(salt‖secret)` (the verifier).

### Laptop configuration (the G560)

Option A — the desktop app (recommended): open **Server** tab, fill in

- **Server URL**: `http://192.168.1.20:8000` (http or https only, no
  embedded credentials),
- **Client ID**: `g560` (lowercase letters/digits/dash, ≤32 chars),
- **Secret**: paste the one-time secret, or browse to a file containing
  it (the field is cleared after saving),
- **Execution**: `LOCAL`, `SERVER`, or `HYBRID` (default HYBRID),

then press **Save & connect**. The settings are stored in
`~/.forge/desktop_client.json`; the secret is stored separately in
`~/.forge/link/g560.secret` with `0600` permissions. Reopening the app
reconnects and restores the server task state automatically
(requirement 9).

Option B — the exact file, written by hand:

`~/.forge/desktop_client.json`:

```json
{
  "version": 1,
  "server_url": "http://192.168.1.20:8000",
  "client_id": "g560",
  "project_id": "demo",
  "mode": "hybrid",
  "request_timeout": 15.0,
  "heartbeat_seconds": 10.0,
  "reconnect": {
    "initial_delay": 1.0,
    "max_delay": 30.0,
    "multiplier": 2.0,
    "max_attempts": 0
  },
  "local": {
    "allow_local": true,
    "allow_server": true,
    "max_task_chars": 2000,
    "min_free_ram_mb": 512,
    "max_files_walked": 5000
  }
}
```

`~/.forge/link/g560.secret` (mode `0600`, single line):

```
<the one-time secret printed by add-client>
```

Field reference:

| Field | Meaning |
|---|---|
| `server_url` | Server base URL. `http`/`https` only; no `user:pass@`; no query/fragment. |
| `client_id` | This desktop's registered identity. |
| `project_id` | Server-side project this desktop is bound to. |
| `mode` | `local` \| `server` \| `hybrid`. |
| `request_timeout` | Per-request timeout in seconds (0.5–300). |
| `heartbeat_seconds` | Liveness probe + auto-reconnect cadence while connected. |
| `reconnect.initial_delay/max_delay/multiplier` | Reconnect ladder: `min(initial·multiplier^n, max)` + ≤10 % jitter. |
| `reconnect.max_attempts` | `0` = retry forever; otherwise the total attempt budget per connect episode. |
| `local.allow_local` / `allow_server` | Policy gates for the router (both sides can refuse). |
| `local.max_task_chars` | LOCAL ceiling on requirement length. |
| `local.min_free_ram_mb` | LOCAL floor on free RAM (probe via `/proc/meminfo`). |
| `local.max_files_walked` | LOCAL ceiling on repository walks. |

### CLI equivalents on the laptop

```bash
forge desktop        # then use the Server tab
python -m forge.desktop_app --project demo=/home/user/demo
```

## Execution modes

### LOCAL — bounded light work only

LOCAL never runs the engineering pipeline. The closed vocabulary
(`forge/client/local_exec.py`) is exactly:

- `repo_summary` — bounded file walk: counts by extension, bytes,
  Python files (≤ `max_files_walked`);
- `git_status` — `git status --porcelain` (argv list, no shell,
  bounded output, 20 s timeout);
- `todo_scan` — TODO/FIXME counts (bounded).

Anything else — including anything model-shaped — is **refused**
(`LocalExecutionRefused`). This is what keeps the G560 light: no model,
no pipeline, no writes, no shell.

### SERVER

The requirement is signed and POSTed to `/api/v1/link/tasks`. The
server's ControlPlane queues it, the dispatcher assigns a worker, and
the full A32 pipeline runs (plan → code → policy gate → apply → test →
debug → review → security → acceptance). The desktop polls state,
streams events into the log, renders approval cards, and shows the
verification gates. The server decides the **effective mode**
(`requested` clamped to the client's `max_mode` ceiling) and records
the client decision alongside the task (`link_task_origins`).

### HYBRID — automatic selection

`forge/client/router.py::decide()` is a pure function with a fixed
gate order; the same inputs always yield the same decision, and every
decision records a reason shown in the desktop log:

1. **mode** `server` → SERVER; `local` → LOCAL if all gates pass, else
   honest refusal (never a silent escalation);
2. **policy** — `allow_local` / `allow_server`;
3. **capability** — only the light vocabulary is local-capable;
4. **model availability** — a task that needs a model is never local
   (the client loads no model by design);
5. **task size** — requirement length vs `max_task_chars`;
6. **resources** — free RAM vs `min_free_ram_mb`, CPU presence;
7. **server reachability** — if the server is down, a *light* task may
   still run locally; an engineering task fails honestly with
   `SERVER_UNREACHABLE` instead of being run somewhere unsafe.

## Security model

- **No plaintext credentials anywhere.** The secret lives only on the
  laptop (0600 file). The server stores `verifier = SHA-256(version |
  "verifier" | salt | secret)` plus a per-client random salt. The
  settings JSON never contains the secret (enforced by an assertion in
  `save()` and by tests).
- **Authenticated handshake.** `challenge` (server nonce + public
  salt) → client proof `HMAC-SHA256(verifier, version|handshake|client|
  nonce_c|nonce_s)`. Challenges are single-use and bounded per client;
  unknown clients get an indistinguishable decoy. The plaintext secret
  cannot be recovered from a stolen server DB. *Documented limitation:*
  the verifier itself is the HMAC key for that one server, so a full
  server-DB attacker could impersonate the client *to that server*
  until rotation (`rotate-secret`). This is the standard verifier-based
  trade-off short of a full PAKE; run the link over TLS or a trusted
  LAN if that matters to you.
- **Authenticated requests.** Every request carries
  `HMAC-SHA256(session_key, version|request|METHOD|path?query|sha256(body)|
  timestamp|nonce)` where both sides derive `session_key` from the
  verifier + both nonces. Timestamps outside ±120 s are rejected;
  every nonce is single-use (replay cache), and only signature-valid
  requests may claim a nonce (an eavesdropper cannot poison one).
  One active session per client: a new handshake supersedes the old.
- **No arbitrary remote command execution.** The link surface is a
  fixed vocabulary of typed endpoints (`/api/v1/link/tasks`, approvals,
  state, info, mutations) — there is no endpoint that accepts commands,
  code, or arbitrary paths. Task text is validated by the existing
  ControlPlane rules; all server-side execution still goes through the
  A33 policy gate, the ChangeSet engine, and deterministic review.
- **Server authorization remains authoritative.** The effective
  permission mode is decided server-side (requested mode clamped to the
  client's registered ceiling). Client-side router decisions only ever
  *tighten* what runs where; the server re-authorized everything that
  actually executes.
- **Bounded everything.** Link bodies ≤ 256 KiB, signed bodies ≤ 2 MiB,
  responses ≤ 8 MiB, bounded walks, bounded logs, bounded replay cache.

## API surface (client-visible)

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/v1/link/challenge` | none | `{client_id, nonce}` → `{server_nonce, salt, ttl, server_time}` |
| `POST /api/v1/link/handshake` | proof | `{client_id, nonce, proof}` → session expiry + server info |
| `POST /api/v1/link/tasks` | signed | submit `{requirement, mode?, execution?, decision?}` |
| `GET /api/v1/link/tasks` | signed | list tasks (with `execution` origin) |
| `GET /api/v1/link/tasks/{id}` | signed | one task (status/stage/model/worker) |
| `GET /api/v1/link/tasks/{id}/logs` | signed | stage logs + timings |
| `GET /api/v1/link/tasks/{id}/events?after=` | signed | event stream (cursor) |
| `GET /api/v1/link/tasks/{id}/verification` | signed | test/review/security/build/acceptance gates |
| `GET /api/v1/link/tasks/{id}/report` | signed | final task report |
| `POST /api/v1/link/tasks/{id}/pause·resume·cancel·retry` | signed | cooperative task control |
| `GET /api/v1/link/approvals` | signed | pending approval requests |
| `POST /api/v1/link/approvals/{id}/decide` | signed | `{approved: bool}` |
| `GET /api/v1/link/state?since=&task=` | signed | one-shot restore snapshot (tasks, queue, approvals, events, info) |
| `GET /api/v1/link/info` | signed | workers, model readiness, protocol version |

## Tests (deterministic)

| File | Covers |
|---|---|
| `tests/test_link_protocol.py` | verifier/proof/session-key math, signature coverage, tamper, windows |
| `tests/test_link_config.py` | settings validation, 0600 secrets, no-secret-in-JSON, reconnect ladder |
| `tests/test_link_connection.py` | state machine, deterministic backoff (fake clock), AUTH_FAILED terminality, heartbeat reconnect |
| `tests/test_link_router.py` | the full HYBRID decision matrix |
| `tests/test_link_local_exec.py` | closed vocabulary, bounds, refusals |
| `tests/test_link_server.py` | handshake flows, replay/tamper/stale rejection, mode clamping, no-exec surface, approvals |
| `tests/test_link_client.py` | client stack end-to-end over the in-process server |
| `tests/test_link_persistence.py` | requirement 8 (server continues) + requirement 9 (reopen restores) |
| `tests/test_desktop_app_link.py` | backend routing (LOCAL/SERVER/HYBRID), snapshot/restore, GUI stub rendering |
| `tests/test_link_manage.py` | server CLI: add/list/rotate/revoke |

## Hardening (post-review pass)

Every item here is pinned by `tests/test_link_hardening.py`.

- **Handshake lockout.** Five failed handshakes within 5 minutes lock
  the client id out (correct credentials included) until the window
  expires; a success clears the counter. Defeats online proof-guessing
  loops without touching unknown-id behavior (decoys stay decoys).
- **Unauthenticated-route rate limit.** `/link/challenge` and
  `/link/handshake` share a tight token bucket (`30` capacity, refill
  `30/min` per source IP) in addition to the lockout.
- **No redirects.** The client transport refuses HTTP 3xx: urllib
  would otherwise re-send the `X-Forge-*` signature headers to the
  redirect target. A 3xx is an honest client-side error; configure the
  real server URL.
- **Single writer.** `LinkService` reuses the ControlPlane's database
  connection instead of opening a second one (no cross-connection
  lock contention on the SQLite file).
- **Race-safe replay cache.** Concurrent duplicate nonces are decided
  by the primary key (both rejected after the first), never an
  `IntegrityError` leaking as a 500.
- **Audited.** Handshakes (success + failure), registrations, and
  client-submitted tasks land in the same JSONL audit trail the
  cockpit uses, under `link-*` actors.
- **Honest error mapping.** Control-plane errors from task submission
  / handshake (validation, conflict, capacity) surface as their own
  4xx codes instead of an opaque 500; garbage query params
  (`?limit=abc`) are typed 400s.
- **`REFUSED` is a first-class outcome.** The router no longer
  silently re-routes: explicit `mode=local` with failing gates, or any
  task when policy forbids every viable path, yields `REFUSED` and the
  client raises instead of executing somewhere the user did not ask
  for. HYBRID remains the only mode that chooses automatically.
- **Fail-closed resource probing.** The probe now really measures:
  `/proc/meminfo` (Linux) → `GlobalMemoryStatusEx` (Windows) →
  `sysconf`+rusage (macOS, deliberately pessimistic). If nothing can
  be measured it reports `unavailable` with zero free RAM, so LOCAL is
  refused for capacity instead of guessed. A crashing probe can never
  take submission down.
- **Secret-file permissions enforced at load.** A group/world-readable
  secret file is tightened to 0600 and then **refused** with a
  rotate-it message — exposure is surfaced, not swallowed. The
  settings-save guard against secret material is a runtime check (it
  holds under `python -O`), not an `assert`.
- **Thread-safe transport.** The UI poller and the heartbeat thread
  share one transport; session state mutates only under a lock and
  concurrent callers collapse into a single handshake.
- **Snapshot/poll contract.** A UI refresh never raises: an expired
  session triggers exactly one in-place reconnect; while the server is
  unreachable the per-poll verification fetch is skipped (no doomed
  requests).
- **No skipped events.** The state-snapshot cursor is the last event
  *returned* (not the store's latest), so a truncated page is resumed
  on the next poll — verified with a 240-event task paged 200 + 40.
- **Bounded, loop-safe local walk.** The LOCAL operations walk lazily
  (a huge repo costs one bounded list, not the whole tree), skip
  symlinked directories and unreadable directories, and never follow
  symlink loops.
- **Validated link inputs.** `execution` labels are a closed set
  (`LOCAL`/`SERVER`/`LOCAL-PLANE`), JSON fields are type-checked, and
  protocol inputs are length-bounded before any regex or DB work.

## Honest limitations

- The desktop→server hop is plain HTTP unless you terminate TLS in
  front (`https://` URLs are accepted and recommended off-LAN).
- LOCAL execution is intentionally minimal; it is a safety valve for
  offline light tasks, not a mini-pipeline.
- Approval decisions and task control are polled (1.2 s UI cadence),
  not pushed; SSE could replace polling later without protocol changes.
- The verifier-based handshake (see above) is not a PAKE.
