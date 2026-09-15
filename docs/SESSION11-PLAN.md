# Session 11 — Plan: Real Inference Fabric & Compute Routing

Base: `main` @ `03b5a85620caef5b00ff09df473cacbf7a2080c2` (Session 10 merged).
Branch: `arena/01a09a74-forge-ai` (this session's fixed branch — see
"Git workflow" below). One PR against `main`, never auto-merged.

## 0. Ground rules inherited from Session 10

* `forge/runtime/model_runtime.py` (**Native Model Runtime**, PR #29) stays
  canonical. Session 11 adds *above and beside* it, never a second runtime.
* `forge/core/resource_governor.py` stays the single budget authority; the
  `g560` profile keeps denying local model loading outright.
* `forge/core/fencing.py` + `forge/core/dag_scheduler.py` stay the execution
  fence authority; inference results are subject to the same fence.
* A32 (hardened loop) and A33 (permission platform) semantics are never
  weakened: the fabric can only *refuse earlier*, never loosen a verdict.
* Nothing claims inference succeeded unless a backend/model actually produced
  the text. No model weights are committed. No silent downloads.

## 1. Canonical architecture (unchanged, now fully wired)

```
Agent
  -> Model Fabric            forge.models  (policy-aware selection)
     -> Routing Engine       forge.models.routing     (explainable decision)
     -> Model Catalog        forge.models.catalog     (one canonical registry)
     -> Residency Cache      forge.models.model_cache (bounded, refcounted)
     -> Backend              forge.models.backends    (stable protocol)
        -> Native Model Runtime   forge.runtime.model_runtime  (canonical)
           -> ModelBackend        native | ollama | llama_cpp | forge | remote
              -> model
```

No agent may import a provider SDK directly; agents keep calling the fabric
(`ModelFabric` / `InferenceFabric`) and the fabric keeps calling the runtime.

## 2. Phases

### Phase A — model identity (spec §3, §20)

`forge/models/identity.py`: `AvailabilityState` (DISCOVERED, CONFIGURED,
LOADED, READY, DEGRADED, UNAVAILABLE, FAILED, POLICY_DENIED,
RESOURCE_DENIED, UNVERIFIED), `VerificationState` (UNVERIFIED, VERIFIED,
FAILED, EXPIRED), `MemoryRequirements`, `ModelIdentity`.

Hard rules encoded as code, not prose:

* `CONFIGURED -> READY` is refused unless `verification_state == VERIFIED`
  (`IdentityError`), and a promotion attempt records the refusal.
* `assert_same_model()` compares `model_id` / family / version / artifact
  fingerprint; a mismatch raises `ModelSpoofingError` — a backend can never
  silently substitute a different model.
* Unknown/unmeasurable values stay empty or `None`; nothing is invented.

`forge/models/catalog.py`: one canonical registry — `register`, `discover`,
`verify`, `load`, `unload`, `status`, `remove`. Removal is reference-aware:
a model with live references is marked `removing` and released when the last
reference drops, so an in-flight request is never invalidated.

### Phase B — backend abstraction (spec §4, §6, §7)

`forge/models/backends.py`: one stable `Backend` protocol with exactly the
required operations — `discover()`, `health()`, `load()`, `unload()`,
`generate()`, `stream()`, `cancel()`, `resource_requirements()` — plus
`BackendStatus` reporting **configured / verified / reachable / ready
separately** (a configured provider is never reported ready).

`RuntimeBackendAdapter` is the canonical implementation: every operation
delegates to a `ModelRuntime` + backend name, so nothing can bypass the
Native Runtime. Concrete adapters: `NativeLocalBackend`,
`OllamaCompatibleBackend` (localhost-aware, network-policy controlled),
`LlamaCppCompatibleBackend`, `ForgeCustomBackend`, `RemoteProviderBackend`.

`forge/models/remote.py`: `RemoteHttpBackend` — a real `ModelBackend`
registered *into* the runtime. HTTPS + TLS validation, destination-IP policy
reused from `forge/security/ssrf.py`, **redirects refused** (a 3xx never
retargets a provider request), bounded response size, bounded timeout,
credentials redacted in every error/log/event path.

`forge/models/reference_engine.py`: a real, first-party, stdlib-only local
inference adapter (`InferenceAdapter` for the runtime's `NativeBackend`) that
loads an actual weight artifact and runs an actual forward pass with real
sampling, streaming, context/output/timeout bounds and cancellation. It ships
**no weights**; an artifact is materialised explicitly
(`ReferenceModelWriter`, used by `forge models create-reference-artifact` and
by tests). It is honestly labelled: a tiny character-level reference engine
that proves the fabric→runtime→backend→model path executes real inference. It
advertises no agentic capability, so it can never be routed a coding task.

### Phase C — verification (spec §3, §20)

`forge/models/verification.py`: `ModelVerifier.verify()` is real, in three
steps — artifact fingerprint (bounded sha256 over the file) when local, a
live backend health probe, and an **identity probe**: one bounded real
generation whose response must be non-empty, error-free, and identity-matched.
Configuration alone never yields `VERIFIED`.

### Phase D — routing engine (spec §8, §9, §10, §16, §17)

`forge/models/routing.py`: `RoutingRequest` inputs (capability, task type,
complexity, context size, latency target, resource budget, privacy
classification, network policy, cost budget, availability, quality, hardware
profile) → `RoutingPlan` outputs (`selected_model`, `selected_backend`,
`reason`, `fallback_path`, `policy_result`, `resource_result`, `factors`,
`candidates`). Explainable by construction: every filter that removed a
candidate is recorded.

The engine *consumes* the Session 10 governor before loading (RAM, model
memory, concurrency, CPU, device profile, network policy) and before
generating (task budget, timeout, output limit, concurrent model slots), and
consults the A33 `ModelDataPolicy` for PUBLIC / INTERNAL / CONFIDENTIAL /
SECRET. Scoring reuses the existing `FabricRouter` signals (reliability,
latency, cost, free-first, local-first, health, complexity) — free-first stays
the default posture and no unlimited frontier inference is ever implied.

`forge/models/fallback.py`: deterministic ladder — preferred verified →
secondary verified → local verified → deterministic non-neural strategy →
honest terminal (`NEEDS_MODEL` / `RESOURCE_DENIED` / `POLICY_DENIED` /
`FAILED` / `TIMEOUT` / `CANCELLED` / `STALE`). A provider, network, timeout or
auth failure stays a failure.

### Phase E — residency, concurrency, streaming, context (spec §11–§14)

* `forge/models/model_cache.py`: one loaded-model registry with reference
  counts, idle timestamps, memory budget, deterministic LRU eviction, and
  single-flight loading (two agents asking for the same model produce one
  load). A model with a live reference is never evicted.
* Fencing: every request carries `task_id`, `attempt_id`, `generation_id`,
  `model_id`, `backend_id`; a result whose fence is no longer authorized is
  discarded as `STALE` and never published.
* `forge/models/streams.py`: bounded streaming — `request_id`, monotonic
  `sequence`, `timestamp`, `model_id`, `delta`, `done`, `error`, plus
  backpressure (bounded buffer, overflow policy), disconnect handling,
  cancellation and a total-character bound. A partial stream is never
  presented as complete unless `done=true`.
* `forge/models/context_budget.py`: deterministic context budget integrated
  with the existing `ContextPack`/`ContextBudgetManager` — rank, compress,
  summarise, drop lowest-value sections, and **record what was omitted**.
  Whole repositories are never dumped into a model.

### Phase F — service surface (spec §21, §22, §23, §29)

* `forge/models/engine.py`: `InferenceFabric` — `generate`, `stream`,
  `cancel`, plus model operations; `InferenceResult` carries full
  observability (§28) and converts to the legacy `ModelResponse`.
* `forge/server/inference.py` + typed API routes: `models.list`,
  `models.status`, `models.verify`, `inference.generate`,
  `inference.stream`, `inference.cancel` — strict schemas
  (`extra="forbid"`), closed operation/scope table, **no command field**.
* `forge/server/client.py`: the G560 thin client gains typed inference
  requests (it sends tasks; it never loads, downloads or hosts models).
* `forge/cli.py`: `forge models list|status|verify|load|unload` and
  `forge infer "..." [--model --backend --stream --json]`; unsafe selections
  are refused with the reason.
* Agents (coder, planner, debugger, reviewer, researcher, documentation,
  security, performance, supervisor, agent-creation) keep requesting models
  through the fabric — no direct provider imports are introduced.

### Phase G — telemetry, evidence, security audit (spec §15, §18, §19, §24)

* Structured telemetry: latency, time-to-first-token, tokens when available,
  success/failure/timeout, policy denial, resource denial, model/backend
  selected, fallback used, verification result. Metadata only.
* `forge/models/evidence.py`: routing evidence → self-improvement
  `Finding`s. Proposals only: permissions, security rules, provider
  authorization and credential policy are never changed by this path.
* Prompt/output boundary: model output is untrusted data and is never a
  shell command, permission grant, approval, network authorization or path
  authorization. Enforced by the existing policy/tool layers and asserted by
  a dedicated inference security audit suite (§18 checklist).

### Phase H — tests, compatibility, docs (spec §25–§30)

New suites (all offline by default): identity/state machine, catalog,
backends + health, verification, residency/LRU/duplicate-load, routing +
governor + privacy, fallback ladder, streaming bounds, context budget,
fencing/stale results, remote provider SSRF/redirect/credential redaction,
reference-engine real inference, server API + reconnect, CLI, G560 denial,
failure injection, and the security audit.

Test policy labels are explicit: `REAL_INFERENCE_TEST` (an actual model ran),
`MOCK_BACKEND_TEST` (a scripted/loopback double), `DETERMINISTIC_TEST`. Live
provider tests are opt-in via `FORGE_REAL_INFERENCE=1` (+ endpoint env) and
are skipped otherwise, so offline CI stays fully functional.

Python 3.8 / Windows 7: `from __future__ import annotations` everywhere,
`typing.Optional/List/Dict/Tuple` for runtime-evaluated positions, no 3.9+
stdlib calls or keyword arguments, no POSIX-only APIs, no new heavyweight
dependency. Gates: `compileall`, `vermin --min-versions=3.8`, AST audit, full
regression suite.

## 3. Git workflow

The user asked for a branch named `arena/SESSION11-real-inference`. This
Arena session is pinned to `arena/01a09a74-forge-ai` (branched from the same
`main` baseline), so all Session 11 work lands there as one coherent ordered
series of commits, with a single PR titled
"Session 11: Real inference fabric & compute routing". `main` is never
modified directly and the PR is never auto-merged.

## 4. Definition of done

See `docs/SESSION11-REPORT.md` §"Acceptance criteria" for the measured
result of each item in the spec's §32 list.
