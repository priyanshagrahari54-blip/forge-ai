# The Canonical Inference Path

Forge has **one** queue, **one** scheduler, **one** permission system, **one**
checkpoint system, **one** runtime, **one** canonical inference path, and **one**
source of truth for whether an attempt is authorized to publish.

This document is the operator-and-implementer reference for that path: what runs
when a model is asked for text, how an attempt is authorized, what happens when
something is misconfigured, and where to look when it does. It describes the
code as it exists — where a behaviour is a policy choice rather than an
accident, the reason is stated.

Related documents: [`SESSION11-REPORT.md`](SESSION11-REPORT.md) (the inference
fabric itself), [`A81-FORGE-SERVER.md`](A81-FORGE-SERVER.md) (the background
loop), [`A81-NATIVE-MODEL-RUNTIME.md`](A81-NATIVE-MODEL-RUNTIME.md) (the
runtime), [`MODEL-FABRIC-INFERENCE.md`](MODEL-FABRIC-INFERENCE.md) (the fabric
seam), [`SESSION11.5-PLAN.md`](SESSION11.5-PLAN.md) (the integration plan).

---

## 1. The path

```
HTTP / CLI / cockpit / voice
        │  (typed request, principal, scope)
        ▼
   ForgeServer ── TaskManager (SQLite) ── TaskQueue ── Scheduler ── WorkerPool
        │                                                    │
        │                                        run_task(): lease → fence →
        │                                        identity → execute
        ▼                                                    ▼
   SupervisorExecutor ──────────────────────────────► Supervisor.run(fabric)
                                                             │
                                                             ▼
                                            ModelFabric.generate(request)
                                                     │  the seam
                              ┌──────────────────────┴───────────────────────┐
                              │ mode = legacy            mode = session11 /  │
                              │                          hybrid (eligible)   │
                              ▼                                              ▼
                    _generate_legacy()                        Session11Adapter →
                    (existing providers,                       InferenceFabric →
                     labelled `legacy`)                        RoutingEngine →
                              │                                ModelCatalog →
                              │                                ResidencyCache →
                              │                                Backend →
                              │                                ModelRuntime →
                              │                                verified model
                              └──────────────┬───────────────────────────────┘
                                             ▼
                                    ModelResponse (+ metadata:
                                    path, model, backend, provider,
                                    neural, deterministic, verification,
                                    fingerprint, identity ids)
```

There is no second router. `ModelFabric.generate()` is the only place that
decides which path answers, and every caller — agents, the Supervisor, the
server's inference API, the CLI — goes through a fabric.

Implementation: `forge/models/inference_path.py` (the seam), `forge/models/
fabric.py` (`generate` / `stream` / `attach_inference_path` / `bind_identity`),
`forge/models/engine.py` (`InferenceFabric`), `forge/server/inference.py`
(`ServerInferenceService`).

## 2. Modes

The mode is **typed and explicit**. It is never inferred from a failure.

| Mode | Meaning |
| --- | --- |
| `legacy` | The pre-Session-11 provider stack answers. Responses are labelled `inference_path="legacy"`. |
| `session11` | The Session-11 inference fabric is the canonical path. A failure there is returned **as a failure**; it never silently degrades to legacy. |
| `hybrid` | Named callers/capabilities stay on legacy; everything else uses Session 11. Each response records which path handled it. |

A "caller" is the request's **stable component label** (`ModelRequest.caller`):
`coder`, `debugger`, `reviewer`, `mediated-agent:<name>`, `agent-bench`. It is
deliberately *not* `ModelRequest.task` — that field is free text (it becomes a
remote system prompt and a routing hint), so keying an allow-list off it would
mean keying configuration off user content. Requests that predate the field fall
back to `task`, and every production call site now declares a label, which
`tests/test_inference_integration_s115.py::test_production_call_sites_declare_a_stable_caller`
enforces so a new call site cannot become unclassifiable by accident.

How the server derives it (`ForgeServer.__init__`):

1. an explicit `ServerConfig.inference_path` (or `FORGE_INFERENCE_PATH`) wins;
2. otherwise `session11` when server inference is enabled, `legacy` when it is
   disabled.

The seam is attached to the fabric only when the mode is not `legacy`, so a
legacy server carries no Session-11 object at all.

Environment switches (all optional, all read once at construction):

| Variable | Effect |
| --- | --- |
| `FORGE_INFERENCE_PATH` | `legacy` \| `session11` \| `hybrid` |
| `FORGE_INFERENCE_PATH_REQUIRE_VERIFIED` | refuse unverified models on the canonical path (default: on) |
| `FORGE_INFERENCE_PATH_ALLOW_DETERMINISTIC` | allow the deterministic non-neural rung to answer |
| `FORGE_INFERENCE_PATH_HYBRID_CAPABILITIES` | capabilities that stay legacy in hybrid mode |
| `FORGE_INFERENCE_PATH_HYBRID_LEGACY_CALLERS` | caller labels that stay legacy in hybrid mode |

Path-level refusals are typed and bounded:
`INFERENCE_PATH_UNAVAILABLE`, `INFERENCE_PATH_DISABLED`,
`INFERENCE_PATH_NOT_ELIGIBLE`.

## 3. Identity

An attempt's identity is minted **once**, in the worker that holds the lease
(`_begin_attempt`), and travels unchanged:

`task_id` · `attempt` · `generation` · `attempt_id` (`"<task>#g<N>"`) ·
`boot_id` · `lease_owner` · `request_id` · `trace_id` · `generation_id` ·
`model` · `backend_id` · `provider`.

`ExecutionIdentity.stamp(request)` fills empty fields; the bound view a worker
uses (`ModelFabric.bind_identity(...)`) stamps with `bind_trace=True`, so the
attempt's trace id **overwrites** a request-local random one. Correlation across
the queue, the executor, the Supervisor, the fabric and the backend is therefore
one id, not a chain of reconstructions. Nothing downstream invents an identity:
a missing field stays missing.

## 4. The fence: who may publish

`forge/core/fencing.py` holds the single authority (`FenceRegistry`,
`AttemptFence`). The server owns one registry and shares it with the inference
service, the workers and the canonical path — there is no per-component copy.

States and legal transitions:

```
QUEUED ──► RUNNING ──► SUCCEEDED | FAILED
  │           │  └────► CANCELLING ──► CANCELLED | FENCED
  │           └───────► TIMED_OUT ──► FENCED
  └───────────────────► FENCED
```

`AUTHORIZED_STATES = {QUEUED, RUNNING}`. Anything else — including
`CANCELLING` — may not publish. `begin()` fences the predecessor generation as
`superseded`, so a retry, a recovery or a new boot takes authority away
atomically. `is_authorized()` consults the **registry's** record, never the
possibly-stale object a worker holds.

### Fail-closed refusals (server inference API)

Uncertainty about authorization is denial, never permission. No model compute
happens, nothing is published, and the caller receives an infrastructure
verdict:

| Situation | Error | Code | HTTP |
| --- | --- | --- | --- |
| no fence authority wired / no attempt for this task | `NoFenceAuthority` | `NO_FENCE_AUTHORITY` | 503 |
| the authority raised while being consulted | `FenceAuthorityError` | `FENCE_ERROR` | 503 |
| attempt fenced, timed out or already terminal | `StaleAttempt` | `STALE_ATTEMPT` | 409 |
| attempt cancelling or cancelled | `CancelledAttempt` | `CANCELLED_ATTEMPT` | 409 |
| a newer generation owns the task | `SupersededAttempt` | `SUPERSEDED_ATTEMPT` | 409 |

An unrecognized fence state is treated as stale: a state the table does not know
is not an authorization.

### Publishing a task outcome

`run_task` asks one question before writing anything
(`_may_publish`, `forge/server/workers.py`): the **lease** and the **fence** must
both authorize this attempt. The check happens *before* the terminal commit,
because committing `SUCCEEDED` is itself terminal. An outcome from an attempt
that lost authority is discarded (`_discard_unauthorized`): the attempt is
fenced, the discard is logged and emitted as `attempt.fenced` metadata, and the
task record is left exactly as its real owner left it.

### Cancellation

One chain, no privileged gap:

```
operator → cancel_task → task record (cancel_requested)
                      → running control (request_cancel)
                      → attempt fence (RUNNING → CANCELLING)   ← authority revoked now
                      → inference boundary (in-flight streams cancelled)
worker notices → confirm_cancelled (CANCELLING → CANCELLED) → task CANCELLED
```

A cancellation before start finishes the job itself (`confirm=True`), because
that attempt will never run again. `FenceRegistry.cancel()` is idempotent, so a
worker confirming an already-cancelling attempt is normal, not an error.

## 5. Provider configuration: visible, never silent

A remote provider moves through three states, and each one is reported
(`ServerInferenceService.provider_configuration()`, embedded in
`inference.status`, `models.status` and `/health`):

| State | Meaning |
| --- | --- |
| `configured` | parsed, validated and registered; reachability is `null` until something probes it |
| `invalid_configuration` | the environment could not be parsed/validated — with the bounded, redacted reason (`FORGE_REMOTE_<ID>_URL is required …`) |
| `unavailable` | registered but its backend cannot serve (policy denial, network disabled, unreachable) |

An invalid provider does not break the server and does not vanish: unrelated
providers keep working, the server still starts, and the operator sees the
cause. What an operator must never see is a bare `NO_MODEL` for a provider that
was merely misconfigured. A provider that has never been probed reports
reachability as **unknown**, not as unreachable — claiming otherwise would be
its own fabrication.

## 6. Durable inference state

Two sources, no invention (`ForgeServer.task_inference_state`):

* the `inference` block persisted with the attempt's result — the authority
  after a restart, when in-memory state is gone;
* the live fence — the authority right now.

Read it at `GET /api/v1/tasks/{task_id}` (inside the task payload) or
`GET /api/v1/tasks/{task_id}/inference`. A task that never ran reports
`persisted: false` and carries **no** `model`, `backend_id`, `path`,
`generation_id` or verification keys: an attempt that never happened has no
provenance to show.

Server-level: `GET /api/v1/status` includes `inference_path` (mode, attached,
`require_verified`, hybrid switches, `boot_id`) and `inference` (service state,
counters, governor snapshot, providers).

## 7. Verification semantics

`CONFIGURED ≠ VERIFIED ≠ READY`. A model is `verified` only after a real probe
generation succeeded against it; auto-verification fires on first **selection**,
not on discovery. `require_verified=true` is not bypassed by background work:
the canonical path refuses an unverified model rather than guessing that it
works. The deterministic non-neural rung is allowed only when an operator turns
`FORGE_INFERENCE_PATH_ALLOW_DETERMINISTIC` on, and every response says whether
it was `neural` and whether it was `deterministic`.

The first-party reference engine advertises **no** capability. Capability
routing therefore honestly refuses it for `coding` work and the deterministic
rung answers instead. That is the invariant holding, not a bug: a tiny character
model must never be selected for engineering work, and the legacy provider must
never be silently substituted either.

## 8. Provenance

Every response carries, at minimum: `inference_path`, `model`, `backend_id`,
`provider`, `neural`, `deterministic`, `verification_state`,
`artifact_fingerprint`, plus the identity ids of §3.

The label does not depend on which door the caller came through: the Session-11
fabric stamps its own results (`InferenceFabric._label_result`), so an agent,
the CLI or a benchmark holding that fabric directly still gets
`inference_path="session11"` rather than being reported as legacy by the honest
fallback. `ModelFabric` stamps the seam's decisions (`session11` / `legacy` /
`hybrid` reason) on top, with `setdefault` semantics: a specific reason already
recorded by the run is never overwritten by a generic one.

Recorded at every production call site: `CoderAgent` and `DebuggerAgent`
(`last_inference` / the Supervisor's `inference_provenance`), `ReviewerAgent`
(`last_inference` — a BLOCK from an unverified deterministic rung and a BLOCK
from a verified neural model are different facts), the mediated agent runtime
(the run record's `inference` block) and the agent benchmark (the `model-smoke`
check's evidence names path, model, backend, neural and verification state). The Supervisor records the
provenance of the calls that mattered, the worker persists a bounded copy with
the task result, and `task.completed` repeats it in the event stream
(`path`, `mode`, `model_id`, `backend_id`, `neural`, `deterministic`,
`verified`, `generation_id`, `request_id`, `error_code`).

Labels are honest: `REAL_INFERENCE_TEST`, `MOCK_BACKEND_TEST`,
`DETERMINISTIC_REFERENCE_MODEL`, `INTEGRATION_TEST`. A mocked backend is never
labelled real inference, and live-provider tests are opt-in
(`FORGE_REAL_INFERENCE=1`) so offline CI stays fully functional.

## 9. Observability bounds

Events, logs, counters and audit records carry identifiers and verdicts. They
never carry prompts, model output, credentials or full change sets; everything
passes the central redaction helper, and every list is bounded (streams retained
for `STREAM_TTL_SECONDS`, provider ledger capped at `MAX_TRACKED_PROVIDERS`,
fence snapshots capped by `limit`).

`FenceRegistry.snapshot(limit=…)` is the bounded, payload-free view of the
authority. Reading health performs **no** model work: `HealthMonitor` describes
the fabric only when one already exists, so a scrape never discovers, verifies,
loads or contacts a model.

Audit is separated from telemetry by failure direction: security-relevant
operations fail closed (a refusal that cannot be recorded is a refusal), while
telemetry may degrade without affecting a verdict.

## 10. Thin clients (G560 and friends)

A Windows 7 / 32-bit / 2 GB device is **never** an inference host. Local
inference is denied by the resource profile; work is delegated to a server over
the typed API. Delegation uses the same control plane as everything else — no
privileged path for voice, cockpit or CLI — and model output crossing that
boundary is untrusted data: there is no shell, command, argv or eval endpoint,
and no server authority is derived from what a model said.

## 11. Compatibility floor

Python 3.8 and Windows 7 remain the floor: no 3.9-only syntax or APIs, no
POSIX-only dependencies, and no `subprocess`/`multiprocessing`/`fork`/`AF_UNIX`
/`fcntl`/`termios` on this path. No weights ship in the repository and nothing
large is downloaded silently; artifact creation stays behind an explicit
operator switch (`FORGE_REFERENCE_CREATE`).
