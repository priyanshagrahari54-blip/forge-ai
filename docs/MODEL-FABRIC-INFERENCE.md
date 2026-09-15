# Model Fabric & Real Inference (Session 11)

The Model Fabric is the **one canonical path** between an agent and a model.
Session 11 made that path real: models have identities that must be *verified*
before they can serve, backends implement one stable protocol, routing is
policy-aware and explainable, residency is bounded and refcounted, streams are
bounded and cancellable, and a request that cannot be served ends in a named
refusal instead of invented text.

```
Agent / CLI / Forge Server
   -> Model Fabric            forge.models            (agent-facing seam)
      -> InferenceFabric      forge.models.engine     (the request path)
         -> RoutingEngine     forge.models.routing    (explainable selection)
         -> ModelCatalog      forge.models.catalog    (one canonical registry)
         -> ResidencyCache    forge.models.model_cache(bounded, refcounted)
         -> Backend           forge.models.backends   (stable protocol)
            -> ModelRuntime   forge.runtime.model_runtime  (canonical, Session 10)
               -> native | ollama | llama_cpp | forge | remote | custom
                  -> model
```

Nothing else may talk to a model. An agent that imports a provider SDK, or a
server route that shells out to a binary, is a bug — see
`docs/A46-MODEL-FABRIC.md` for the older fabric and
`docs/A81-NATIVE-MODEL-RUNTIME.md` for the runtime that stays canonical.

## 1. The honesty contract

These are invariants, not aspirations. Each one is enforced in code and has a
test that fails if it is weakened.

| Rule | Where it is enforced |
|---|---|
| A model is `DISCOVERED` until a real check proves it. `CONFIGURED -> READY` without verification raises `IdentityError`. | `identity.set_availability` |
| Verification is real: artifact fingerprint + backend health probe + one identity probe generation. | `verification.ModelVerifier.verify` |
| A reported model/fingerprint that differs from the registered one is spoofing, not a substitute. | `identity.assert_same_model` |
| Nothing is downloaded, and no weights ship in the repository. Artifact creation is an explicit operator action. | `reference_engine.ReferenceArtifactWriter`, `FORGE_REFERENCE_CREATE` |
| A provider failure, timeout, auth failure or network failure never becomes model output. | `engine._run_step`, `fallback.classify_error` |
| The fallback ladder ends in `NEEDS_MODEL` / `RESOURCE_DENIED` / `POLICY_DENIED` / `UNVERIFIED` — never in fabricated text. | `fallback.FallbackLadder`, `engine._run_ladder` |
| `neural=True` only when a real backend produced the text. The deterministic rung is always `neural=False`. | `engine._finish` |
| SECRET data never reaches an external provider, whatever the caller declared. | `routing._data_policy_verdict` |
| G560 (Win7 / 32-bit / 2 GB) never loads a model. | `resource_governor` + `routing._check_resources` |
| A stale attempt publishes nothing. | `fencing.commit_guard` + `engine._fence_reason` |
| No prompt, context, credential or model text reaches logs, telemetry, events or audit records. | `telemetry`, `server.inference._STREAM_SUMMARY_KEYS`, `_notify` |

## 2. Model identity

`forge/models/identity.py`

```
AvailabilityState: DISCOVERED CONFIGURED LOADED READY DEGRADED
                   UNAVAILABLE FAILED POLICY_DENIED RESOURCE_DENIED UNVERIFIED
VerificationState: UNVERIFIED VERIFIED FAILED EXPIRED
```

* `usable` is an **availability** statement only (`LOADED`, `READY`, …). It
  never implies verification.
* `verified` is the separate gate the router applies when
  `require_verified=True` (the default).
* Every transition is recorded in `identity.history` with a reason, so "why is
  this model not ready?" has an answer that is not a guess.
* `ModelIdentity.to_dict()` is the canonical wire shape (model id, provider,
  backend, family/version, context and output limits, capabilities,
  quantization, parameter count, locality, memory requirements, states,
  fingerprint, cost posture, quality/latency evidence).

## 3. Backends

`forge/models/backends.py` — one protocol, eight operations:

```python
discover() health(probe=True) load(model_id) unload(model_id)
generate(request, token=None) stream(request, token=None) cancel(reason)
resource_requirements(model_id)
```

`BackendStatus` keeps four questions separate — `configured`, `reachable`,
`verified` — and **derives** `ready` from them (`ready = configured and
reachable and verified and not denial`). A backend is never "ready" because it
was configured. See `docs/BACKENDS.md` for the adapter list and how to add one.

## 4. Verification

`forge/models/verification.py`

1. **Artifact fingerprint** (local models): sha256 over the bytes, bounded at
   64 MB, compared against the fingerprint recorded at registration. A changed
   artifact fails with `FINGERPRINT_MISMATCH` and demotes the identity to
   `FAILED` / `unverified` — the new bytes are never silently adopted.
2. **Backend reachability**: a real health probe. A governor/network denial is
   reported as a denial, not as "unreachable".
3. **Identity probe**: one real, bounded generation. The response's reported
   model must match the registered identity (`assert_same_model`), and an empty
   completion fails the check.

Outcomes are `VERIFIED`, `FAILED` or `EXPIRED` (TTL). Verification is cached
per backend (`mark_verified`) with a TTL, so a long-lived server does not
re-probe on every request.

The probe carries the verifier's own prompt
(`"Forge model verification probe. Reply briefly."`, 24 output tokens, 30 s
bound) — never the caller's text — so proving a model cannot leak request
content, and proving a remote model cannot send classified data.

`auto_verify` (CLI `--auto-verify`, service
`FORGE_SERVER_INFERENCE_AUTO_VERIFY`) runs **before** routing: selection refuses
an unverified model, so verifying after selection would make the flag dead.
`InferenceFabric._auto_prepare()` verifies the named model, or up to
`AUTO_VERIFY_MAX` (4) unverified candidates for the requested
capability/backend, and records an `auto_verify` observation per attempt. It
skips non-local candidates when the request itself forbids outbound traffic
(`network_policy="off"`, or a declared `secret`/`confidential`
classification): routing then refuses the request without Forge having talked
to the provider first.

## 5. Routing: explainable, policy-aware

`forge/models/routing.py`

Every decision produces a `RoutingPlan` that answers, in data:

```
selected_model, selected_backend, reason, reasons[], state, error_code,
score, factors{}, fallback_path[], considered[], rejected[], bounds{},
policy_result{}, resource_result{}, ladder{}
```

Candidate filters, in order (each rejection carries a human-readable reason
and a denial class):

1. **Capability** — never relaxed. A model that does not advertise the
   capability is refused, not substituted.
2. **Data policy (A33)** — `SECRET`/`CONFIDENTIAL` may not reach a non-local
   provider; the classification is *detected* from the text as well as
   declared, so declaring "public" cannot launder a credential.
3. **Network policy and device profile** — `off` / `server-only` / `explicit`
   for remote candidates (`allow_remote` first); for local candidates the
   profile's `model_loading_allowed` (G560 refuses local model loading
   outright, and records the denial on the candidate's own resource verdict).
4. **Availability / verification** — identity-level `POLICY_DENIED` /
   `RESOURCE_DENIED`, unusable states, and `require_verified`.
5. **Context fit** — the model's real window against the request's need.
6. **Cost posture** — free-first: a paid backend needs `allow_paid`;
   `max_cost_per_token` caps per-token price; a remaining cost budget that
   cannot cover one token of a model refuses it (exact arithmetic, never an
   estimate of output length).
7. **Latency target** — a gross miss filters, a close miss only scores lower.
8. **Resource governor** — model memory, RAM, concurrency slots, cost budget,
   CPU threads, device profile.

Absolute policy is evaluated **before** verification on purpose: a candidate
that policy forbids must never be reported as merely "unverified", which would
tell an operator to prove a model Forge is not allowed to use. The converse
also holds in `_no_candidate()`: when one candidate was refused for
verification, that candidate *cleared* capability and absolute policy, so the
headline state is `UNVERIFIED` ("run `forge models verify`") even if another
candidate was refused by policy.

Scoring prefers verified, local, free, low-latency, high-quality candidates;
the factors are reported so the choice can be argued with.

## 6. The fallback ladder

`forge/models/fallback.py`

```
preferred_verified -> secondary_verified -> local_verified -> deterministic -> terminal
```

* Neural rungs are tried in order; a cancellation, a fence or a deadline is
  **terminal** (the next rung would only burn a dead attempt's budget).
* The deterministic rung is a local, non-neural strategy
  (`LocalModelProvider`) that says out loud that no model answered. It is
  reported as `model_id="deterministic:local-fallback"`,
  `backend_id="deterministic"`, `finish_reason="deterministic"`,
  `neural=False`, and `fallback.used=True`.
* `allow_deterministic=False` means "model output or nothing": the attempt
  ends in `NEEDS_MODEL` with empty text.
* `classify_error()` maps error codes onto honest terminal states
  (`TIMEOUT`, `CANCELLED`, `STALE`, `RESOURCE_DENIED`, `POLICY_DENIED`,
  `UNVERIFIED`, `NEEDS_MODEL`, `FAILED`).

## 7. Residency

`forge/models/model_cache.py`

* **Single flight**: concurrent requests for one model load it once; the others
  wait and are counted in `duplicate_loads_avoided`.
* **Refcounts**: `acquire()` / `release()`, plus a `residency()` context
  manager that always releases (even when the request raises).
* **In-use models are never evicted.** `remove()` on a referenced model marks
  `pending_remove`; a new reference cancels it.
* **Bounds**: `max_slots`, `max_bytes`, `idle_seconds`. Pressure evicts idle
  entries first, then least-recently-used, then refuses honestly
  (`RESIDENCY_FULL` / `RESIDENCY_BUDGET`) with the names of the models holding
  the slots.
* **Three different things are counted separately**: `loads`, `evictions`,
  `unloads`, and `refusals` (a refusal is only ever a genuine denial — an
  operator unload is bookkeeping, not a refusal).
* A failed load leaves no phantom model: the entry is marked `failed` and a
  later attempt really tries again.

## 8. Streaming

`forge/models/streams.py`

* `BoundedStream` is bounded on every axis: buffer size (`max_buffer`),
  total characters (`max_total_chars`), producer put timeout, consumer get
  timeout.
* Sequence numbers are strictly monotonic; exactly one terminal event is
  delivered (`done=True`), and a terminal event is **always** queued — if the
  buffer is full it displaces an undrained delta, and the displaced characters
  are counted in `dropped_chars`.
* Backpressure that never drains becomes a loud `STREAM_OVERFLOW` failure, not
  silent loss.
* `cancel()` and `close()` are terminal and idempotent; a producer whose
  consumer walked away stops instead of leaking a thread.
* `snapshot()` is metadata only — never the streamed text.
* `join_deltas()` reports `text`, `done`, `complete`, `partial`,
  `monotonic_sequence`: a stream that never terminated is reported as partial
  whatever text arrived.

## 9. Context budget

`forge/models/context_budget.py`

Sections are labelled and typed
(`system` 100 > `prompt` 95 > `task` 90 > `instructions` 80 > `constraints` 78 >
`repository` 60 > `research` 50 > `memory` 40 > `attachment` 30), ranked by
`required` first, then kind priority, then relevance score, then kind/key — a
total deterministic order. Only `repository`, `research`, `memory`,
`attachment` and `instructions` are compressible; a `required` section that does
not fit makes the plan **infeasible** (an honest refusal beats a silently broken
prompt).

Bounds: 4 chars/token, an assumed 4096-token window when a backend reports none
(recorded as `assumed_limit`), 1024 tokens per section, 24 sections. The budget
is the model's real window minus the reserved output tokens.

The plan reports `context_limit`, `assumed_limit`, `reserved_output_tokens`,
`budget_tokens`, `estimated_tokens`, `omitted_tokens`, `feasible`, `reason`,
`included[]`, `omitted[]` (with a reason each) and `compressed[]`; `render()`
produces the prompt and states which sections were left out.

## 10. Fencing

Inference results are subject to the same execution fence as everything else
(Session 10, `forge/core/fencing.py`):

```python
fabric.generate(request, task_id=..., attempt_id=..., fence=fence,
                fence_registry=registry)
```

The fence is checked **before** spending compute, after the generation, and
between stream chunks. A superseded attempt returns `state="stale"`,
`error_code="STALE"`, empty text, `neural=False`, and its result is dropped.
`ServerInferenceService(fences=registry)` binds a server generation to the
task's current fence, so an attempt cancelled or superseded mid-flight cannot
publish.

## 11. Evidence feed (self-improvement input)

`fabric.evidence()` returns a bounded diagnostic summary: request counts,
success/failure/timeout rates, resource and policy denials, fallbacks, stale
results, p50/p95 latency, per-model breakdowns, error codes and **proposals**.

`evidence_to_findings()` turns it into `Finding` objects for the
self-improvement loop. Its authority string is part of the payload:

> diagnostic only: routing proposals may not change permissions, security
> rules, provider authorization, or credential policy

Evidence never contains prompt or response text.

## 12. Data classification and privacy

* `classify_text()` detects credential-shaped material (API keys, tokens,
  private keys, connection strings) and raises the classification of a request
  even when the caller declared it public.
* `SECRET` never leaves the machine: a non-local candidate is refused with
  `POLICY_DENIED` and the reason `secret data may not reach an external
  provider (deny)`.
* Model output is **untrusted data**: `scan_model_output()` flags instruction
  overrides, role hijacks, shell commands, permission grants, credential
  requests, tool invocations and exfiltration URLs. Flags are informational —
  they never authorize an action, and nothing executes the text.
* Telemetry records ids, states, latencies, token counts, `text_chars` and
  output flags — never the text.

## 13. Server surface

`forge/server/inference.py`, routes in `forge/server/api.py`.

```
GET  /api/v1/models?discover=1&backend=&capability=&usable_only=1
GET  /api/v1/models/status?model_id=
POST /api/v1/models/verify     {model_id?, backend_id?}
POST /api/v1/models/load       {model_id, timeout?}
POST /api/v1/models/unload     {model_id, force?}
POST /api/v1/inference/generate{prompt, context?, capability?, model?, ...}
POST /api/v1/inference/stream  (same schema) -> {stream_id, request_id, events}
GET  /api/v1/inference/streams/{stream_id}/events?after=&wait=
POST /api/v1/inference/cancel  {request_id, reason?}
GET  /api/v1/inference/status
```

* Inference is **disabled by default**. A disabled service answers
  `503 INFERENCE_NOT_CONFIGURED`; enable it with `FORGE_SERVER_INFERENCE=1`
  (plus `..._REFERENCE_DIRS` / `..._MODEL_DIRS` / `..._ALLOW_NETWORK`).
* Schemas are strict (`_Strict`): unknown fields are rejected, bounds are
  enforced (`MAX_PROMPT_CHARS` 32 K, `MAX_CONTEXT_CHARS` 128 K,
  `MAX_OUTPUT_TOKENS` 4096, `MAX_TIMEOUT_SECONDS` 300, `MAX_STREAM_CHARS`
  128 K, `MAX_EVENTS_PER_STREAM` 2048, `MAX_RETAINED_STREAMS` 64,
  `STREAM_TTL_SECONDS` 300).
* There is **no arbitrary command endpoint**. No field accepts a command,
  script, argv or code payload, and `reject_execution_vectors()` refuses
  execution-shaped keys even before the schema is consulted.
* Scopes: `models:read`, `models:control`, `inference:run`,
  `inference:control` (`viewer` gets read-only). Every route declares exactly
  one operation from the closed `API_OPERATIONS` table.
* Concurrency is bounded; a service at its limit **refuses** (`403`) instead of
  queueing without bound.
* Stream events are replayable from a cursor (no duplicates, no gaps); the
  completion summary is text-free metadata (`_STREAM_SUMMARY_KEYS`).

## 14. CLI

```
forge models discover|verify|status|load|unload|backends|evidence|create-reference
forge infer "prompt" [--stream] [--json] [--explain] [--verify]
                     [--strict-neural] [--no-deterministic]
                     [--allow-unverified] [--capability C] [--model M]
                     [--server URL --token T] [--resource-profile g560]
```

* Every invocation builds a fresh registry, so `discover` runs implicitly
  unless `--no-discover` is given; the CLI says so when a command's answer is
  per-process (residency, evidence).
* `--explain` prints the routing reason, the selected and rejected candidates,
  the resource and policy verdicts, the context plan, the ladder and the
  observations.
* `--strict-neural` exits 1 (and says why) when the answer did not come from a
  model.
* `--server URL` is the G560 thin-client path: no local models, the server does
  the work, and the printed provenance is the server's.

## 15. Agent integration

`forge/models/fabric_bridge.py` keeps the agent-facing seam intact:

```python
from forge.models import ModelFabric
from forge.models.fabric_bridge import attach_inference, detach_inference

names = attach_inference(legacy_fabric, inference_fabric)   # verified only
...
detach_inference(legacy_fabric, names)
```

Agents keep calling `ModelFabric`; the bridge mirrors **verified** inference
models into it as providers and preserves provenance in
`response.metadata` (`neural`, `deterministic`, `backend_id`,
`inference_model_id`, `verification_state`, `availability_state`,
`output_flags`, `generation_id`, `state`). An unverified model is never
mirrored, so an agent cannot be handed a model nobody proved.

## 16. Reference engine (real local inference, no weights shipped)

`forge/models/reference_engine.py`

A tiny first-party character-level network, written and run entirely by Forge:

* `ReferenceArtifactWriter.write(path, config)` materialises a deterministic
  artifact (default 600,032 bytes for the default shape) — pseudo-random
  weights from a seed, no download, no training claim. Creation is explicit
  (`forge models create-reference PATH`, or `FORGE_REFERENCE_CREATE=1`).
* `ReferenceLocalBackend` discovers `.forgeref` artifacts, health-checks them,
  loads them into bounded residency, and runs a **real forward pass** for
  `generate`/`stream` (threaded incremental delivery, bounded queue).
* It advertises `capabilities=()`: capability-based routing can never select
  it. It exists to prove the path end to end, honestly labelled
  (`trained=False`, "no agentic capability").

This is what makes the offline test suite able to say "real inference" without
shipping or downloading a model.

## 17. What is deliberately not done

* No model is ever downloaded implicitly; no weights are committed.
* No second runtime: the Native Model Runtime stays canonical and the fabric
  adapts to it.
* No capability relaxation, no silent model substitution, no "close enough"
  answer.
* No prompt injection defence that pretends to be a guarantee: model output is
  scanned, flagged and treated as untrusted data.
* No cost metering: the cost budget is accounting over caller-reported spend,
  plus exact per-token refusals.
