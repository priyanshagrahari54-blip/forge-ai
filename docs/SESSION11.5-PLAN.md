# Session 11.5 — Plan: real inference integration, durable execution, fail-closed hardening

Branch: `arena/01a09a74-forge-ai` (this Arena session is pinned to it, so Session 11.5
lands on the same branch and the same PR as Session 11 — **PR #36**). `main` is never
modified directly and nothing is auto-merged.

Base commit at the start of this session: `c2f3a29` (Session 11 complete, PR #36 open).

## 0. Rule for this session

One queue. One scheduler. One permission system. One checkpoint system. One runtime.
One canonical inference path. One source of truth for whether an attempt may publish.

Nothing here adds a second of any of those. Where a boundary is missing, the existing
component gains a typed seam; where behaviour is unsafe, the existing component is
corrected in place.

## 1. Baseline — what the code actually does today (traced, not assumed)

Every line below was verified by reading the current tree at `c2f3a29`.

### 1.1 The server's real background path (legacy)

```text
POST /api/v1/tasks                       forge/server/api.py
  → TaskManager.create (SQLite)          forge/server/tasks.py
  → TaskQueue.enqueue (SQLite)           forge/server/queue.py:32
Scheduler._loop / dispatch_once          forge/server/scheduler.py:101,122
  → TaskQueue.lease_next(project, owner) forge/server/queue.py:67   (lease carries boot_id)
  → WorkerPool.submit(run_task)          forge/server/workers.py:54
run_task                                 forge/server/workers.py:104
  → queue.lease_held_by(task_id, owner)  workers.py:128 (start), :171 (mid-run), :231 (release)
  → server.executor.execute(ExecutionContext)
SupervisorExecutor.execute               forge/server/executor.py:221
  → fabric = self.fabric or server.fabric                            executor.py:227
  → Supervisor(...).run(..., fabric=fabric, ...)                     executor.py:254-263
Supervisor.run                           forge/core/supervisor.py:112
  → agents (CoderAgent, DebuggerAgent, ReviewerAgent, ...)
  → self.fabric.generate(ModelRequest(...))   coder.py:492,525 · debugger.py:217
                                              reviewer.py:79 · mediation.py:866
ModelFabric.generate                     forge/models/fabric.py:233
  → FabricRouter.route → ProviderRegistry → provider.generate
  → deterministic local fallback at the end of the failover chain
```

`server.fabric` is `ModelFabric.from_defaults()` (`forge/server/server.py:221`) and the
executor is built with it (`server.py:227`). **That is the legacy router.**

### 1.2 Where Session 11 actually lives today

```text
ServerInferenceService                   forge/server/inference.py:231
  → .fabric (lazy)                       inference.py:266  → build_inference_fabric()
InferenceFabric                          forge/models/engine.py:363
  → RoutingEngine → ModelCatalog → ModelResidencyCache → Backend → ModelRuntime
InferenceFabricProvider / attach_inference  forge/models/fabric_bridge.py:37,140
```

Reachable from: the HTTP inference endpoints (`/api/v1/inference/*`, `/api/v1/models/*`)
and the CLI (`forge infer`, `forge models`).

**Not reachable from the background loop.** `attach_inference` has exactly two
non-definition references in the whole tree — the export in `forge/models/__init__.py`
and `tests/test_inference_fabric_routing.py:508,513,529`. No production caller. That is
the "opt-in isolation" problem this session removes.

### 1.3 Fencing today

* `AttemptFence` / `FenceRegistry` — `forge/core/fencing.py:101,134`, with
  `begin/mark_running/transition/cancel/confirm_cancelled/timeout/commit/current/
  generation_of/seed_generation/is_authorized`.
* Owner: `DAGScheduler.registry` — `forge/core/dag_scheduler.py:186`. Used by
  `forge/core/orchestrator.py:346,409` and `forge/cli.py:2538,2559,2587,2635`.
* The **server's queue/worker loop does not use it.** Its restart fence is the lease
  (`owner` + `boot_id`, revalidated by `lease_held_by`).
* `ServerInferenceService` accepts `fences=` (`inference.py:237,249`) and the server
  constructs it **without** that argument (`server.py:251`) → `self.fences is None` in
  production, so no generation is attempt-bound today.
* `_fence_for` (`inference.py:678-688`) catches every exception from the fence authority
  and returns `None`, i.e. *fence error → no fence → continue*. Fail-open. §6 kills this.

### 1.4 Identity today

`ModelRequest` already carries `trace_id`, `task_id`, `attempt_id`, `generation_id`,
`model`, `backend`, `classification`, `network_policy`, `hardware_profile`
(`forge/models/request.py:36-60`, added by Session 11) — and agents set none of the
execution-identity fields. `Supervisor.run` has no identity parameter; it mints its own
`run_id = uuid4().hex` (`supervisor.py:207`). `run_task` knows the attempt only as an
ordinal, `task.retry_count + 1` (`workers.py:138`).

### 1.5 Other confirmed defects this session fixes

| # | Where | Defect |
|---|---|---|
| §15 | `forge/server/inference.py` `_build()` (~line 320) | `RemoteProviderConfig.from_env` failures are swallowed (`_ = exc`, "simply not registered") → an invalid provider vanishes instead of becoming `INVALID_CONFIGURATION`. |
| §13 | audit/telemetry call sites | audit writes wrapped in broad `except` → a lost audit record is indistinguishable from a recorded one. |
| §16 | governor / pool / semaphore / residency | four independent bounds, no declared owner, no exposed denial owner. |
| §10 | task persistence | no inference identity/state on the durable task record. |

### 1.6 Architecture BEFORE

```text
User → Cockpit/CLI/API/Voice → typed task submission → SQLite TaskQueue
     → Scheduler (leases) → WorkerPool → run_task → SupervisorExecutor
     → Supervisor → agents → ModelFabric (LEGACY router) → providers
                                                       ↘ deterministic fallback
Session-11 InferenceFabric  ── only via HTTP /api/v1/inference/* and `forge infer`
FenceRegistry               ── only via DAGScheduler / orchestrator / CLI
```

Two model-selection paths exist. They never meet. Nothing in the background loop is
attempt-fenced at the inference boundary.

### 1.7 Architecture AFTER (target of this session)

```text
User → Cockpit/CLI/API/Voice → typed task submission → SQLite TaskQueue (unchanged)
     → Scheduler (leases, unchanged) → WorkerPool → run_task
        ├─ begins an AttemptFence in the server's FenceRegistry (existing class)
        └─ binds ExecutionIdentity(task_id, attempt_id, trace_id, lease/boot)
     → SupervisorExecutor → Supervisor → agents
     → ModelFabric.generate  ← ONE authoritative selection path
        └─ InferencePath decision (legacy | session11 | hybrid) — recorded, never silent
           → Session11Adapter → InferenceFabric → RoutingEngine → ModelCatalog
              → ModelResidencyCache → Backend → Native ModelRuntime → verified model
        └─ fence validated before compute, at chunk boundaries, before publication
     → ModelResponse (legacy vocabulary) + full Session-11 provenance in metadata
     → durable task result: inference path/model/backend/verification/generation ids
     → audit (fail-closed for security-sensitive ops) + telemetry (degrades visibly)
```

## 2. Delivery slices (each independently tested, each one commit)

| Slice | Content | Spec sections |
|---|---|---|
| **A** | `forge/models/inference_path.py`: typed mode (`legacy`/`session11`/`hybrid`), path decision, Session-11 → legacy `ModelResponse` adapter, identity-bound fabric view. `ModelFabric.generate/stream` delegate through it. Provenance metadata on every response. | §2 §3 §19 §20 §21 |
| **B** | Server fence authority: `ForgeServer` owns a `FenceRegistry` (existing class) and passes it to `ServerInferenceService`; `run_task` begins/commits/cancels an attempt fence; `_fence_for` fails **closed** with typed codes (`NO_FENCE_AUTHORITY`, `FENCE_ERROR`, `STALE_ATTEMPT`, `CANCELLED_ATTEMPT`, `SUPERSEDED_ATTEMPT`); identity threaded queue → worker → executor → supervisor → fabric → inference → result → audit. | §5 §6 §7 §8 §9 |
| **C** | Durable inference state on the task record + result provenance + resume-after-disconnect/restart semantics; zombie-late-publish refusal. | §10 §11 §12 §24 |
| **D** | Audit vs telemetry classification (fail-closed audit for security-sensitive operations, visible degradation for telemetry), audit correlation fields, centralized redaction. | §13 §14 |
| **E** | Provider configuration visibility (`CONFIGURED → INVALID_CONFIGURATION → UNAVAILABLE` with bounded redacted reason, surfaced in `models.status` and health). | §15 |
| **F** | Resource hierarchy: declared owner per layer, denial carries its owner, single reserve→execute→release lifecycle, residency reference release on stale/cancel/exception/shutdown. | §16 §17 §18 |
| **G** | Streaming correctness under the new path: monotonic sequence, bounded buffer, cursor replay without duplication, overflow state, stale attempt → `STALE`. | §23 |
| **H** | G560 thin client preserved and tested end to end (`g560 infer` → local denied → server delegation succeeds). | §26 |
| **I** | Call-site audit of every model call (canonical / legacy / test-only / deterministic / dead / duplicate) and isolation of duplicates. | §22 |
| **J** | CLI provenance (`forge tasks`/`forge run`/`forge infer` answering: which model, why, verified?, backend?, neural?, denied?, retried?, stale?). | §32 |
| **K** | Observability: bounded histories for queue/workers/inference/models/residency/routing/verification/fences/resources. | §33 |
| **L** | Performance measurement before/after (measured, not estimated). | §25 |
| **M** | Docs: `SESSION11.5-PLAN.md` (this file), `INFERENCE-INTEGRATION.md`, `SESSION11.5-REPORT.md`. | §34 §37 |
| **N** | Leftover from the Session-11 follow-up: operations reporting as a shipped feature — typed `GET/POST /api/v1/inference/report`, `forge ops report`, cockpit panel, tests. | (carry-over) |

### Delivery status

Commits are on `arena/01a09a74-forge-ai` (PR #36). Test counts are for
`tests/test_inference_integration_s115.py` unless noted.

| Slice | Status | Commit | Notes |
|---|---|---|---|
| **A** | done | `700f333` | canonical seam, typed modes, adapter, identity-bound view, provenance |
| **B** | done | `700f333` | one `FenceRegistry` on the server, fail-closed `_fence_for`, worker fence lifecycle |
| **E** | done | `ce30675` | provider ladder + `inference` health component + `FenceRegistry.snapshot()`; 4 tests |
| **C** | done | `e6ff51f` | `task_inference_state` on the task API; **and** the two §8 defects it exposed (below); 5 tests |
| **M** | partial | `9767d31` | `docs/INFERENCE-PATH.md` written; `SESSION11.5-REPORT.md` still owed (§37) |
| **I** | done | `9c6d0af` | stable `caller` labels, provenance at reviewer/mediation/bench, self-labelling results; 3 tests + 1 corrected |
| **D** | not started | — | audit vs telemetry classification (§13/§14) |
| **F** | not started | — | resource hierarchy, denial owner, residency release (§16–§18) |
| **G** | not started | — | streaming correctness under the new path (§23) |
| **H** | not started | — | G560 thin-client end-to-end delegation (§26) |
| **J** | not started | — | CLI provenance (§32) |
| **K** | not started | — | bounded observability histories (§33) |
| **L** | not started | — | measured performance before/after (§25) |
| **N** | not started | — | leftover operations-reporting feature (carry-over) |

Two defects Slice C found while making inference state readable — both were
publish-authority holes, not reporting holes:

1. `cancel_task` never reached the fence. It set `cancel_requested` and signalled
   the running control, leaving the attempt `RUNNING` and therefore *authorized*:
   a worker that finished before noticing the cancel could commit `SUCCEEDED` and
   publish a result the operator had just forbidden. Cancellation now moves the
   current fence to `CANCELLING` immediately (pre-start cancellations confirm
   `CANCELLED` themselves).
2. Publication was gated on the queue **lease** only, by a helper named
   `_fenced()` that never looked at a fence. `_may_publish()` now requires the
   lease *and* an authorized attempt, checked **before** the terminal commit
   (committing `SUCCEEDED` is itself terminal, so checking after would always
   refuse). An unauthorized outcome is discarded: the attempt is fenced, the
   discard is logged and emitted as `attempt.fenced`, and the task record is left
   to whoever really owns it.

## 3. Invariants that may not be weakened

1. `CONFIGURED ≠ VERIFIED`, `DISCOVERED ≠ READY`; READY requires real verification.
2. No fabricated output: a failure stays a failure; a deterministic answer is labelled
   `neural=false, deterministic=true`.
3. Uncertainty about authorization is denial, never permission.
4. Never silently choose the legacy path because Session 11 failed. Every request records
   which path served it and why.
5. A stale/superseded/cancelled attempt may finish physically but its output never becomes
   authoritative; the result is `STALE`, not `COMPLETED`.
6. No credentials, prompts, private context or full model output in logs/events/audit/
   telemetry/test artifacts.
7. No arbitrary command execution in any inference route; model output stays untrusted data.
8. G560 (Win7 32-bit, 2 GB) never becomes an inference host.
9. Python 3.8 grammar/stdlib floor; Windows 7-safe APIs; no `multiprocessing`/`fork`/
   `AF_UNIX`/`fcntl`/`termios`; no new dependencies.
10. The durable TaskQueue, the worker pool, the Scheduler, A32/A33, the checkpoint system
    and the Native Model Runtime remain canonical — extended, never replaced.

## 4. Test labelling convention (§30)

Every test added by this session declares one of:

```text
REAL_INFERENCE_TEST              a real forward pass happened
DETERMINISTIC_REFERENCE_MODEL    the reference engine produced it (not a production model)
MOCK_BACKEND_TEST                a double answered; no model compute
INTEGRATION_TEST                 real production classes, end to end
```

Live-network tests stay opt-in (`FORGE_REAL_INFERENCE=1`); offline CI remains fully
functional. The reference engine is a real forward pass over a tiny trained-on-nothing
character model — it is never described as a production foundation model.
