# Session 10 — Final Report: Full Integration, Hardening & Stabilization

Branch: `arena/01a09944-forge-ai` (branched from current main `d2a109d`).
One PR against main; not merged automatically.

## Result in one line

Every subsystem now runs on ONE canonical path — Agent → Model Router/
Fabric → Native Runtime → Backend → Model — with execution fencing,
durable terminal states, a unified resource governor, an honest
training lab, a true thin client with replay-resistant login, and a
failure-injected test matrix. **2582 passed, 3 skipped** on the actual
tree (measured, not inherited).

## Architecture summary

### 1. Dependency-aware DAG scheduler with execution fencing (new canonical)

- `forge/core/fencing.py` — `AttemptFence` + CAS-verified `FenceRegistry`.
  Every attempt has a unique `(task_id, generation)` identity; `begin()`
  fences the previous generation, so **retries never overlap abandoned
  attempts**. Only the authorized generation may commit a terminal state;
  stale attempts raise `StaleAttemptError`. `commit_guard(fence)` is the
  write choke point: `""` while authorized, a reason once fenced.
- `forge/core/dag_scheduler.py` — dependency-aware execution with bounded
  workers, resource locks (conflict → deterministic `LOCK_CONFLICT`, no
  deadlocks), per-task watchdog, cooperative + grace-fenced cancellation,
  and a durable SQLite store (tasks, attempts, append-only events with a
  monotonically sequenced `seq`). Recovery fences orphaned `RUNNING`
  attempts from a dead process, re-queues retryable work within budget,
  and **never resurrects a terminal state**.
- `MultiAgentOrchestrator.execute` runs its step DAG through the scheduler
  (no second engine); a timed-out step is fenced and its late result is
  rejected. `ChangeApplier` / `ToolRuntime` / `GatedAgentRuntime` /
  `CoderAgent` accept a `commit_guard`, so a fenced attempt's writes are
  refused at every choke point and exactly its own files roll back.
- **Cancel hardening (found by failure injection):** `orchestrator.cancel()`
  now drives the scheduler-level `cancel_all` (CANCELLING + grace fence),
  not only the cooperative per-attempt path — a worker that ignores
  checkpoints can no longer defeat cancellation. A run whose steps all
  ended cancelled is reported `CANCELLED`, never `SUCCEEDED`.

### 2. A83's 14 hardening fixes ported into the canonical agent engine

`forge/agents/creation.py`, `mediation.py`, `specs.py`,
`forge/tools/checkpoint.py`, `forge/tools/change_applier.py`:
actual-changed-file verification (`observed_changes`), symlink-escape
containment, fail-closed model-identity probe, all-refused ≠ success,
`history(limit=0)` ⇒ empty, oversized-store refusal, newest-first
benchmark history, honest `remove()`, create-race lock registry
(per-store-path `_STORE_LOCKS` + stat guard for cross-process), atomic
writes (unique temp + fsync + `os.replace`), PolicyGate verdict kept
separate from the tool outcome, refused calls consume no budget,
bookkeeping failures surfaced, and integrity-bound approved manifests
(hand-edited spec ⇒ fingerprint mismatch ⇒ reset/refuse).

### 3. Unified resource governor + G560 profile (new canonical)

`forge/core/resource_governor.py` — one budget surface: CPU, RAM, disk,
network policy, concurrency, model memory, cost budget, wall-clock time.

- Profiles: `default` and `g560` (2 GB thin client: **local model
  loading denied outright**, concurrency clamped to 2, network
  server-only, 128 MB scratch). Auto-detection is advisory
  (≤ 2.5 GB ⇒ g560-class); an explicit profile (`FORGE_RESOURCE_PROFILE`
  / `--profile`) always wins; **unknown profiles fail closed** to the
  restrictive bounds.
- Wired into the canonical execution points: `ModelRuntime.load()`
  refuses governor-denied loads before any backend is touched
  (`RuntimeCapacityError`); the Forge Server clamps its worker pool to
  the profile; the desktop runtime auto-selects the device class, so a
  thin client never becomes an inference machine.
- Concurrency is a `BoundedSemaphore` — over-booking is impossible by
  construction. Unmeasurable hardware is reported as `None`
  (`measured=False`), never invented.

### 4. G560 true thin client (new)

- `forge/server/client.py` — typed client: challenge/response login,
  whoami, status/health, projects, typed task submission
  (`{project_id, requirement, mode, priority, max_retries}` — **no
  command field exists in the protocol**), task/events (cursor replay),
  approvals. Stdlib only.
- `forge/server/auth.py` + `api.py` — `GET /auth/challenge` issues a
  single-use, time-bounded nonce (bounded tracking, expired entries
  evicted); `POST /auth/sessions` binds to it and consumes it, so a
  captured session request cannot be replayed.
  `FORGE_SERVER_REQUIRE_CHALLENGE` makes the handshake mandatory.
- `DesktopBackend` connection modes: `local` (embedded plane — current
  behavior, unchanged) and `server` (thin: projects/tasks/events/
  approvals route through the server; **no local plane is created in
  server mode**). Server states map explicitly onto the plane
  vocabulary the UI understands. `forge server login` performs the
  exchange from the CLI.
- No bypass is possible: in server mode the client only ever sends
  typed requests; the server's strict schemas (`extra="forbid"`) and A33
  gates still decide everything.

### 5. Honest training lab + coherent CLI

- `forge tasks list|show|events [--store PATH|DIR] [--json]` — inspects
  the durable fenced-scheduler store (the control plane writes one store
  per orchestration under `<project>/.forge/tasks/<id>.db`, so
  deterministic step ids from repeated runs can never collide).
  Monotonic `seq` is verifiable from the CLI.
- `forge training scan|export|start AGENT [--runs runs.json] [--model M]
  [--authorize]` and `forge training job JOB_ID` — every refusal (no
  data, no `OPENAI_API_KEY`, policy `deny`, secret material) is reported
  honestly; **a job is claimed only when one was actually submitted**.
  Secret material is refused in every mode, including `allow` +
  `--authorize`.

## Files changed (31 files, +6105/−224 vs main)

Source (17):
`forge/core/fencing.py` (new, 403), `forge/core/dag_scheduler.py`
(new, 1119), `forge/core/resource_governor.py` (new, 443),
`forge/core/orchestrator.py`, `forge/agents/creation.py`,
`forge/agents/mediation.py`, `forge/agents/specs.py`,
`forge/agents/execution.py`, `forge/agents/coder.py`,
`forge/agents/debugger.py`, `forge/tools/checkpoint.py`,
`forge/tools/change_applier.py`, `forge/core/supervisor.py`,
`forge/runtime/model_runtime.py`, `forge/runtime/runtime.py`,
`forge/control/control_plane.py`, `forge/server/{auth,api,server,client}.py`
(client new, 231), `forge/desktop_app/{backend,app}.py`, `forge/cli.py`.

Tests (7 new suites, 2,256 lines):
`test_scheduler_fencing.py`, `test_dag_scheduler.py`, `test_a83_ports.py`,
`test_cli_tasks_training.py`, `test_resource_governor.py`,
`test_g560_thin_client.py`, `test_failure_injection.py`.

Docs (2): `SESSION10-PLAN.md`, `SESSION10-REPORT.md` (this file).

No second implementation of any existing subsystem was created; the
native model runtime (PR #29) remains canonical and is only *gated* by
the governor. No branch was merged wholesale; PR #22/#24/#25/#28/#30
served as design sources only.

## Tests run (actual tree, Python 3.11.2, pytest 8.3.5)

- **Full suite: 2582 passed, 3 skipped** (263 s), zero failures.
- New suites: 11 (fencing) + 20 (DAG) + 20 (A83 ports) + 11
  (CLI tasks/training) + 18 (governor) + 8 (thin client) + 7
  (failure injection) = **95 new tests**, all passing.
- Failure matrix (spec categories) and where each is covered:
  worker timeout/fence + stale result → `test_scheduler_fencing`;
  worker crash, crash-then-retry, hang+cancel, retry-during-shutdown,
  SQLite store failure, event-sequence race → `test_failure_injection`;
  lock conflict + bounded concurrency → `test_dag_scheduler`;
  checkpoint ordering + partial-apply rollback → `test_a32_transaction`;
  malformed/tampered manifest → `test_a83_ports`; approval mismatch →
  `test_a32_transaction`/`test_a38_api`; model/backend unavailable →
  `test_a81_*`; server restart → `test_server_restart`; client
  reconnect → A81 reconnect + cursor replay in `test_g560_thin_client`;
  network failure → doctor + research honesty suites.

## Security findings

1. **Cancellation could be defeated by a hung worker** (fixed, Phase E):
   `orchestrator.cancel()` only used the cooperative per-attempt path; a
   worker ignoring checkpoints finished and its result was accepted.
   Now: scheduler-level cancel → CANCELLING → grace fence; late result
   rejected as stale.
2. **All-cancelled runs reported SUCCEEDED** (fixed, Phase E): report
   status now derives from the step outcomes; cancelled ⇒ `CANCELLED`,
   `accepted=False`.
3. **Repeated orchestrations could collide in a shared durable store**
   (fixed, Phase C): deterministic step ids + one shared store would
   reject the second run (`task already exists`). Fixed by scoping the
   durable store per orchestration (`<project>/.forge/tasks/<id>.db`).
4. **Challenge nonces are single-use and time-bounded** (added, D2):
   a captured `POST /auth/sessions` cannot be replayed; unknown/expired
  /consumed nonces are refused; tracking is bounded (1024).
5. **Unknown resource profiles fail closed** to the restrictive g560
   bounds instead of guessing (added, D1).
6. No hardcoded credentials found in the diff (secret scan clean);
   all server tokens are generated at bootstrap and compared in
   constant time.
7. Pre-existing, unchanged: the runtime's redaction of secrets in error
   text, the research engine's SSRF/https-only bounds, and the server's
   strict request schemas (no arbitrary command field; `reject_execution_vectors`).

## Compatibility findings (Python 3.8 / Windows 7)

- `python -m compileall forge/ tests/` — clean.
- `vermin --min-versions=3.8 forge/` — **zero findings** (whole tree).
- AST audit of the whole tree: no runtime-evaluated PEP 604 unions or
  PEP 585 builtin generics without `from __future__ import annotations`;
  no 3.9+ stdlib calls (`removeprefix`, `functools.cache`, `math.lcm`,
  `zoneinfo`, dict `|` merge) in changed files.
- Governor and client use stdlib only (`/proc/meminfo` on Linux,
  `GlobalMemoryStatusEx` via ctypes on Windows, `shutil.disk_usage`);
  unmeasurable values degrade to `None`, never a crash.
- Windows note: per-store-path locks are in-process; a cross-process
  create race is bounded by the stat guard (documented in
  `creation.py`). SQLite multi-process access uses the default busy
  timeout.

## Remaining limitations (honest)

- **A hung worker thread outlives `run()`** by Python's nature (threads
  cannot be killed). It is fenced — its commits and guarded writes are
  refused — and the pool join waits for it; a truly infinite worker
  still delays shutdown by its remaining runtime. Documented in
  `dag_scheduler.run`, covered by the hang+cancel test.
- **HYBRID desktop mode is not a separate implementation**: LOCAL and
  SERVER modes are shipped; "hybrid" is expressed as local plane +
  server connection coexisting, where the server still governs anything
  it executes. A dedicated hybrid scheduler would be a second engine
  and was deliberately not built.
- **Cost budget enforcement is accounting, not metering**: the governor
  tracks spend reported by callers; a provider that reports nothing
  spends nothing.
- **Model memory on the default profile is unbounded** (`0`); set
  `model_memory_mb` in a profile (or the runtime's `max_resident_bytes`)
  to cap it. The g560 profile caps it at zero by design.
- **G560 detection is advisory memory-based** (≤ 2.5 GB); explicit
  profile selection is always honored.
- The desktop UI shows the governor/connection state in the runtime and
  status panels; a dedicated G560 "offload" workflow screen is not
  built (the mode + routing + governor behavior is, and is tested).

## Commits (main → HEAD)

```
7b03882 Session 10 (E): failure injection + cancel hardening
4ee4fe6 Session 10 (D2): G560 true thin client with challenge/response login
6e08a97 Session 10 (D1): unified resource governor with G560 thin-client profile
d1465f4 Session 10 (C): honest training lab + fenced-scheduler task CLIs
50677eb Session 10 (B): port A83's 14 hardening fixes into the canonical agent engine
0280a58 Session 10 (A2): thread execution fencing into all write choke points; run A38 on the fenced DAG scheduler
79b889e Session 10 (A): canonical fenced DAG scheduler
```

Final commit: **7b03882**.
