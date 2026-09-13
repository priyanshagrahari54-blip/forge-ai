# Session 10 — Full Integration, Hardening & Stabilization: Work Plan

Goal: ONE coherent, secure, maintainable Forge architecture. No duplicated
subsystems, no fake functionality. Every change is implemented and tested.

## Status: COMPLETE

All phases implemented, tested, and committed (see `SESSION10-REPORT.md`
for the final report): A (fenced DAG scheduler, `79b889e`/`0280a58`),
B (A83 port, `50677eb`), C (training/training CLIs, `d1465f4`),
D1 (resource governor + G560 profile, `6e08a97`), D2 (true thin client
+ challenge/response, `4ee4fe6`), E (failure injection + cancel
hardening, `7b03882`). Full suite: **2582 passed, 3 skipped**.

## Canonical map (what exists on main and stays canonical)

| Subsystem            | Canonical location                                   |
|----------------------|------------------------------------------------------|
| Native Model Runtime | `forge/runtime/model_runtime.py` (PR #29)            |
| Model Fabric         | `forge/models/` + opt-in `forge/models/runtime_bridge.py` |
| A32 ChangeSet        | `forge/tools/change_applier.py`                      |
| A33 PolicyGate       | `forge/security/policy_gate.py`, `policy.py`, `permissions.py`, `approvals.py` |
| Supervisor           | `forge/core/supervisor.py`                           |
| Multi-agent DAG      | `forge/core/orchestrator.py` (A38)                   |
| Server (remote auth) | `forge/server/` (A81 server)                         |
| Agent Creation       | `forge/agents/creation.py`, `specs.py`, `mediation.py` |
| Self-improvement     | `forge/self_development/`                            |
| Memory               | `forge/memory/` (SQLite engine)                      |
| Research             | `forge/research/` (secure engine + SSRF)             |
| Checkpoints          | `forge/tools/checkpoint.py`                          |
| Desktop (G560)       | `forge/desktop_app/`                                 |

## Phase A — Dependency-aware scheduler with execution fencing (Section 3)

New canonical modules:

1. `forge/core/fencing.py`
   - `AttemptFence` — identity for one execution attempt:
     `task_id`, `attempt_no`, `generation`, `state`.
   - `FenceRegistry` — thread-safe, CAS-verified:
     - `begin(task_id) -> AttemptFence` (bumps generation; the previous
       generation is fenced immediately — retries never overlap).
     - `commit(task_id, fence, terminal, payload) -> bool` — only the
       currently-authorized generation may commit a terminal state.
       Stale attempts raise `StaleAttemptError`.
     - `fence(task_id, fence, reason)` — invalidates the attempt
       (timeout/cancel/crash); every later commit or guarded write fails.
     - State machine: `QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLING
       → CANCELLED | TIMED_OUT → FENCED`. Terminal states immutable.
   - `commit_guard(fence)` — callable for the write choke points:
     returns `""` while authorized, a reason string once fenced.

2. `forge/core/dag_scheduler.py`
   - `DAGScheduler` — dependency-aware DAG execution:
     - bounded worker pool, topological gating, deterministic terminal
       states (same plan shape as A38).
     - per-task watchdog: timeout → `TIMED_OUT` → `FENCED`; the worker
       thread may still be alive but its commit is rejected and its guard
       refuses further writes.
     - cancellation: `cancel(task_id)` → `CANCELLING`; the attempt's
       `SupervisorControl` is cancelled (stage-boundary stop + rollback);
       confirmation → `CANCELLED` + fenced.
     - resource locks: named exclusive locks with conflict detection
       (a task needing a held lock fails with `LOCK_CONFLICT` instead of
       deadlocking, unless `wait_for_locks` with a bound).
     - persistence: SQLite (`scheduler.db` or a supplied store path):
       tasks + attempts + an append-only `events` table with a monotonic
       `seq` (single-writer, durable). Terminal states survive restart;
       `recover()` fences orphaned `RUNNING` attempts (their worker died)
       and re-queues retryable work within the attempt budget. Never
       resurrects a terminal task.
     - `on_event` callback + `events(task_id)` for observability:
       task_id, attempt_id, generation, phase, state, reason, ts, seq.

3. Integration (no API break):
   - `MultiAgentOrchestrator.execute` runs its step DAG through a
     `DAGScheduler`; each step attempt gets an `AttemptFence` + bound
     `SupervisorControl`; a step that times out is fenced — a late step
     result is rejected and never recorded as success.
   - `AgentRequest` gains `guard: Any = None` (fence guard callable).
   - Write choke points accept an optional `commit_guard`:
     - `ChangeApplier.apply(..., commit_guard=None)` — checked before the
       checkpoint and before every write; a fenced attempt aborts and
       rolls back exactly the files it touched (or none).
     - `ToolRuntime.execute(..., commit_guard=None)` — mutating tools
       refuse while fenced.
     - `CoderAgent` / `Supervisor.run` / `GatedAgentRuntime.run` thread
       the guard through.

## Phase B — A83 hardening ported into the canonical engine (Section 6)

Port (not copy) the 14 PR #30 fixes into `forge/agents/mediation.py`,
`forge/agents/creation.py`, `forge/tools/change_applier.py`:

1. Verification inspects ACTUAL changed files: checkpoint snapshot diff
   (`observed_changes`) instead of declared tool paths; terminal-tool
   writes are verified and rolled back.
2. Symlink escape: resolve-and-contain check in the mediated path
   authorization (fail explicitly, not via a deep exception).
3. Model identity fails closed: `is_fallback_response` probe; any probe
   exception treats the response as a fallback (refused when the spec
   forbids it).
4. All actions refused ⇒ run is not a success (explicit stage/error).
5. `history(limit=0)` ⇒ empty list, never "everything" (benchmark
   `BenchmarkStore.history`, runtime history, any other limit-taking
   history).
6. Oversized files refused on read (agent store, memory store, manifests).
7. Benchmark history ordered newest-first by recorded_at (not by id).
8. `remove()` verifies the target is actually gone; partial delete is a
   failure, never a success.
9. `create()` race: concurrent same-name create fails closed (store-stat
   check + atomic write; explicit test).
10. Atomic writes: unique temp file in the same directory + fsync +
    `os.replace`, temp cleaned on failure.
11. PolicyGate verdict recorded separately from the tool outcome — the
    gate's own decision is never overwritten by the handler result.
12. Budget counters charge only authorized work (a refused call consumes
    no tool/write budget).
13. Bookkeeping failures surface: run result carries `recorded` + notes
    when the history/manifest write failed.
14. Approved manifest integrity: the stored package records the spec
    fingerprint at write time; load and run refuse a manifest whose spec
    no longer matches its recorded fingerprint (hand-edited spec ⇒ not
    approved ⇒ reset/refuse).

## Phase C — G560 thin client (Section 9)

- `forge/server/challenge.py` (server side): nonce challenge +
  HMAC challenge/response, single-use nonces with TTL (replay
  protection), bound to the existing AuthManager/SessionManager.
- `forge/desktop_app/server_client.py` (client side): typed protocol
  envelope `{v, op, request_id, ts, nonce, payload}` over a CLOSED op
  vocabulary (no arbitrary commands): health, whoami, projects,
  task CRUD, pause/resume/cancel, approvals, events (cursor replay),
  logs, memory overview, model/runtime status.
- `DesktopBackend` modes: `local` (current embedded plane), `server`
  (all execution remote), `hybrid` (local lightweight state + remote
  execution). Mode never bypasses server-authoritative policy: in
  server/hybrid mode the client sends typed requests only; the server's
  A33 gates still decide.
- `forge desktop --server URL` + device profile (G560: 2 GB RAM ⇒ no
  local inference; model work goes to the server/remote provider).

## Phase D — Resource governor (Section 16)

- `forge/core/resource_governor.py` — stdlib-only:
  - RAM/CPU/disk probes with graceful degradation (values stay 0 when
    unmeasurable — never invented).
  - `DeviceProfile` (g560: max_ram_mb=2048, inference_allowed=False):
    a low-RAM device refuses local inference backends in the runtime and
    routes heavy work to the server.
  - bounded-concurrency accounting used by the scheduler/governors.

## Phase E — Training lab honesty (Section 13)

- Extend canonical `forge/agents/training.py` with: dataset ingestion
  (from real run outcomes), filtering, PII/secret boundary (existing
  policy), `TrainerAdapter` abstraction (LoRA/QLoRA parameter surface),
  explicit `MockTrainerAdapter` (labeled mock, produces no artifact),
  model registry with artifact fingerprints, candidate comparison,
  promotion/rollback. `claims_trained()` is True only when an artifact
  with a matching fingerprint exists on disk.
- CLI: `forge training ...`

## Phase F — CLI (Section 24)

- `forge tasks` — list/status of durable tasks from the canonical
  scheduler store.
- `forge training` — training lab surface.
- (existing: status, run, doctor, plan, analyze, models, runtime, agents,
  memory, research, self-improve, serve, server, desktop)

## Phase G — Integration verification

- Memory: verify the canonical engine covers sessions/tasks/projects/
  failures/decisions/agents/models/research/performance/fixes with
  provenance + redaction (extend only where a type is missing).
- Self-improvement: verify candidate isolation + ledger + iteration cap
  in `forge/self_development/`; close gaps only where real.
- Research: keep as-is (already secure), add regression tests if thin.

## Phase H — Test matrix + audits

New suites:
- `tests/test_dag_scheduler.py` — DAG, bounded concurrency, dependencies,
  deterministic terminal states.
- `tests/test_scheduler_fencing.py` — stale results rejected, timeout
  fences, late writes refused after cancel/timeout, retry never overlaps
  the abandoned attempt, recovery never resurrects terminals, event seq
  monotonic + durable.
- `tests/test_failure_injection.py` — worker timeout/hang/crash, lock
  conflict, checkpoint failure, partial filesystem write, partial delete,
  malformed manifest, approval mismatch, model unavailable, backend
  unavailable, server restart, client reconnect, SQLite failure, event
  sequence conflict, network failure.
- `tests/test_a83_hardening.py` — the 14 regressions on the canonical
  engine.
- `tests/test_g560_client.py` — typed protocol, challenge/response,
  replay refused, mode selection, no-bypass.
- `tests/test_resource_governor.py`, `tests/test_training_lab.py`.
- `tests/test_cli_tasks_training.py`.

Audits:
- `python -m compileall forge`
- `vermin -t=3.8-` over `forge/`
- secret scan of the diff
- full pytest suite (measure the actual tree, not historical claims)

## Git workflow

Work on `arena/01a09944-forge-ai` (this session's pinned branch, branched
from current main). Incremental commits per phase. ONE PR against main at
the end; not merged automatically.
