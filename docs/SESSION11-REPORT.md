# Session 11 — Final Report: Real Inference Fabric & Compute Routing

Branch `arena/01a09a74-forge-ai`, base `03b5a85` (main at session start).
Spec: `docs/SESSION11-PLAN.md`. Guides: `docs/MODEL-FABRIC-INFERENCE.md`,
`docs/BACKENDS.md`.

## Result in one line

The Model Fabric is now a **real** inference path: models carry identities that
must be proven by an actual check before they can serve, eight backends speak
one stable protocol over the canonical Native Model Runtime, routing is
policy-aware and explainable, residency and streams are bounded and cancellable,
a request that cannot be served ends in a named refusal
(`NEEDS_MODEL` / `RESOURCE_DENIED` / `POLICY_DENIED` / `UNVERIFIED`) instead of
invented text — and the whole path is exercised offline by a first-party
reference engine that runs a real forward pass over an explicitly created
600 KB artifact, with **no weights shipped and nothing ever downloaded**.

Full suite: **2802 passed, 6 skipped, 0 failed** (477 s, Python 3.11.2,
pytest 8.3.5), up from 2582 passed / 3 skipped at the base: **+220 tests**.

## Architecture summary

### 1. Model identity contract (`forge/models/identity.py`, 652)

`AvailabilityState` (DISCOVERED, CONFIGURED, LOADED, READY, DEGRADED,
UNAVAILABLE, FAILED, POLICY_DENIED, RESOURCE_DENIED, UNVERIFIED) and
`VerificationState` (UNVERIFIED, VERIFIED, FAILED, EXPIRED) are separate axes.
`usable` is an availability statement only; `verified` is the gate routing
applies. `set_availability()` enforces the transition rules: promoting to a
verification-gated state without `verification_state == verified` raises
`IdentityError(code="UNVERIFIED_PROMOTION")`, so **CONFIGURED never becomes
READY silently**. `assert_same_model()` treats a differing reported model or
fingerprint as spoofing, not as a substitute. Every transition is recorded in
`history` with a reason.

### 2. One backend protocol (`forge/models/backends.py`, 829)

`discover / health / load / unload / generate / stream / cancel /
resource_requirements`, with `BackendStatus` keeping `configured`, `reachable`,
`verified` and `denial` apart and **deriving** `ready` (`recompute()`). The
defaults are honest: an unimplemented `discover()` finds nothing, `health()`
reports "not implemented", and `generate()`/`stream()` raise
`BackendNotReadyError`. Adapters: `NativeLocalBackend`,
`OllamaCompatibleBackend` (loopback-only guard), `LlamaCppCompatibleBackend`,
`ForgeCustomBackend`, `RemoteProviderBackend`, plus any custom runtime backend
exposed with its own locality. `InferenceFabric.from_runtime()` registers only
backends the runtime really has. See `docs/BACKENDS.md`.

### 3. Real verification (`forge/models/verification.py`, 364)

Three checks, all real: **artifact fingerprint** (sha256, bounded 64 MB,
compared with the registered value), **backend reachability** (a probe, with a
governor/network denial reported as a denial, not as "unreachable"), and an
**identity probe** — one bounded generation whose reported model must match the
registered identity and whose completion must be non-empty. The probe sends the
verifier's own prompt, never the caller's text. Outcomes are cached with a TTL
(`mark_verified` / `mark_unverified`). A tampered artifact yields
`FINGERPRINT_MISMATCH`, demotes the identity and refuses generation.

### 4. Canonical registry + bounded residency (`catalog.py` 665, `model_cache.py` 535)

One registry (`ModelCatalog`) over one bounded, refcounted cache: single-flight
loads (`duplicate_loads_avoided`), `acquire`/`release` and a `residency()`
context manager that always releases, in-use models never evicted
(`pending_remove` cancelled by a new reference), bounds on slots / bytes / idle
time, pressure eviction idle-first then LRU, and honest `RESIDENCY_FULL` /
`RESIDENCY_BUDGET` refusals naming the models holding the slots. `loads`,
`evictions`, `unloads` and `refusals` are counted separately — an operator
unload is bookkeeping, not a refusal. Discovery refresh never carries a stale
verification across a changed fingerprint.

### 5. Bounded streaming (`forge/models/streams.py`, 456)

`BoundedStream` bounds buffer, total characters, producer put timeout and
consumer get timeout; sequence numbers are strictly monotonic; exactly one
terminal event is delivered and it is **always** queued (displacing an
undrained delta, counted in `dropped_chars`) so a consumer can never hang on a
finished stream; backpressure that never drains fails loudly as
`STREAM_OVERFLOW`; `cancel()`/`close()` are terminal and idempotent; a producer
whose consumer walked away stops instead of leaking a thread; `snapshot()` is
metadata only. `join_deltas()` reports `complete` / `partial` separately — a
stream that never terminated is partial whatever text arrived.

### 6. Context budget (`forge/models/context_budget.py`, 451)

Labelled, typed sections ranked by a documented total order (required →
kind priority → score → kind → key), compressed or omitted to fit the model's
**real** window minus reserved output tokens (4 chars/token, assumed 4096 when
unreported and flagged as an assumption, 1024 tokens/section, 24 sections). A
required section that does not fit makes the plan **infeasible**; `render()`
states which sections were left out.

### 7. Explainable routing (`forge/models/routing.py`, 1185)

Every decision returns a `RoutingPlan` with `selected_model`,
`selected_backend`, `reason`, `reasons[]`, `state`, `error_code`, `score`,
`factors{}`, `fallback_path[]`, `considered[]`, `rejected[]` (each with its
reason and denial class), `bounds{}`, `policy_result{}`, `resource_result{}` and
the ladder. Filters run capability (never relaxed) → **A33 data policy** →
**network policy / device profile** → availability & verification → context fit
→ free-first cost (with an exact per-token budget check) → latency target →
resource governor. Absolute policy precedes verification deliberately: a
candidate policy forbids must never be reported as merely "unverified". When a
candidate *was* refused for verification it had cleared policy, so
`_no_candidate()` headlines `UNVERIFIED` ("run `forge models verify`") even if
another candidate was refused by policy.

### 8. Honest fallback ladder (`forge/models/fallback.py`, 303)

`preferred_verified → secondary_verified → local_verified → deterministic →
terminal`, built from the ranked candidates (max 6 steps). Cancellation, a fence
or a deadline is terminal — the next rung would only burn a dead attempt's
budget. `classify_error()` maps failures onto terminal states through a closed
code map (flags win over string matching). The deterministic rung is a local
non-neural strategy reported as `model_id="deterministic:local-fallback"`,
`backend_id="deterministic"`, `finish_reason="deterministic"`, `neural=False`,
`fallback.used=True`; with `allow_deterministic=False` the attempt ends in
`NEEDS_MODEL` with empty text. Nothing in the ladder can synthesize output.

### 9. Reference engine — real local inference (`reference_engine.py`, 1002)

A tiny first-party character-level network: `ReferenceArtifactWriter.write()`
materialises a deterministic artifact (600,032 bytes for the default shape,
`trained=False`) from a seed — creation is an explicit operator action
(`forge models create-reference`, or `FORGE_REFERENCE_CREATE=1`); nothing is
downloaded. `ReferenceLocalBackend` discovers `.forgeref` artifacts,
health-checks them, loads them into bounded residency and runs a **real forward
pass** for `generate`/`stream` (threaded incremental delivery through a bounded
queue). It advertises `capabilities=()`, so capability routing can never select
it — it proves the path, it does not pretend to be an assistant.

### 10. Remote providers + hardened requester (`remote.py` 814, `security/ssrf.py` +200)

`RemoteProviderConfig` is explicit configuration only (URL, model, key env,
timeout, response/stream bounds, host allowlist, capabilities, context window,
cost). `to_dict()` is a loggable view (`api_key_set`, never the key);
credential-bearing headers in the config raise `RuntimeSecurityError`; URLs with
embedded credentials are refused. `fetch_policy()` is the single source of truth
for the pre-flight destination check *and* the send (https-only, public
destinations, `max_redirects=0`, bounded body), honouring exactly two
operator-declared exceptions derived from the configured URL: an explicit
`allow_http` on a loopback host, and an explicit non-default port.
`ssrf.request()` is the new API requester: closed method set (GET/POST),
redirects refused outright, JSON/text content-type check, bounded body, identity
encoding only, destination pinned to the validated IP, TLS validated against the
real hostname, hardening headers the caller cannot override, and audit text
scrubbed of URL userinfo and credential-shaped values. Reported model mismatch
is refused as spoofing; an empty completion is a protocol failure; a 401/403 is
"rejected the credentials"; a timeout is counted as a timeout. Streaming is a
bounded **chunked replay** of one request (`streamed_by="chunked-replay"`), with
`max_stream_chars` bounding the total and the cut reported
(`truncated=True`, `provider_chars`, `finish_reason="length"`).

### 11. Fencing, telemetry, evidence (`engine.py` 1923, `evidence.py` 131)

Fences (task/attempt/generation) are checked before spending compute, after the
generation and between stream chunks; a superseded attempt returns
`state="stale"`, `error_code="STALE"`, empty text, and publishes nothing.
Telemetry records ids, states, latencies, token counts, `text_chars` and output
flags — never prompt or completion text. `fabric.evidence()` produces a bounded
diagnostic summary (rates, denials, fallbacks, stale results, p50/p95 latency,
per-model breakdowns, error codes, proposals) whose authority string is part of
the payload: *"diagnostic only: routing proposals may not change permissions,
security rules, provider authorization, or credential policy"*.
`evidence_to_findings()` feeds the self-improvement loop.

### 12. Server surface (`forge/server/inference.py` 863, `api.py` +169, `authorization.py`, `client.py` +247, `server.py` +33)

Ten typed routes (`/models`, `/models/status`, `/models/verify|load|unload`,
`/inference/generate|stream|cancel`, `/inference/streams/{id}/events`,
`/inference/status`), strict schemas that reject unknown fields, bounds
(32 K prompt, 128 K context, 4096 output tokens, 300 s timeout, 128 K stream
chars, 2048 events/stream, 64 retained streams, 300 s TTL), bounded concurrency
that **refuses** at the limit, scopes `models:read` / `models:control` /
`inference:run` / `inference:control`, cursor-based event replay with no
duplicates or gaps, text-free completion summaries, fences bound at admission,
and **no arbitrary command endpoint** — `reject_execution_vectors()` refuses
execution-shaped keys before the schema is consulted. Inference is disabled by
default (`503 INFERENCE_NOT_CONFIGURED`) and enabled with
`FORGE_SERVER_INFERENCE=1` plus its `_*` knobs. The typed client gained
`generate`, `stream`, `stream_events`, `cancel_inference`, `inference_status`.

### 13. CLI and agents (`forge/cli.py` +890, `fabric_bridge.py` 254)

`forge models {discover,verify,status,load,unload,backends,evidence,
create-reference}` and `forge infer` (local fabric or the G560 thin-client
`--server` path) with honest provenance, `--explain`, `--strict-neural` and
refusal exit codes (1). `attach_inference()` / `detach_inference()` mirror only
**verified** inference models into the agent-facing `ModelFabric` and preserve
provenance in `response.metadata` (`neural`, `deterministic`, `backend_id`,
`inference_model_id`, `verification_state`, `availability_state`,
`output_flags`, `generation_id`, `state`), so agents keep one seam and can never
be handed a model nobody proved. Model output is scanned
(`scan_model_output()`) and treated as untrusted data: flags are informational
and never authorize an action.

## Files changed (34 files, +17,366 / −3 vs base `03b5a85`)

New source (15): `forge/models/{identity 652, backends 829, verification 364,
catalog 665, model_cache 535, streams 456, context_budget 451, fallback 303,
routing 1185, engine 1923, reference_engine 1002, remote 814, evidence 131,
fabric_bridge 254}.py`, `forge/server/inference.py` (863).

Modified source (10): `forge/cli.py` (+890), `forge/models/__init__.py` (+159),
`forge/security/ssrf.py` (+200), `forge/server/client.py` (+247),
`forge/server/api.py` (+169), `forge/models/request.py` (+46),
`forge/server/server.py` (+33), `forge/server/authorization.py` (+19),
`forge/models/fabric.py` (+8), `forge/models/provider.py` (+4).

Tests (9 new files, 5,164 lines, 223 test functions): `helpers_s11.py` (504),
`test_inference_identity_backends.py` (348/18),
`test_inference_residency_streams.py` (602/30),
`test_inference_fabric_routing.py` (540/29),
`test_inference_reference_engine.py` (481/22),
`test_inference_remote_provider.py` (767/39),
`test_inference_server_api.py` (732/28), `test_inference_cli.py` (517/33),
`test_inference_live.py` (673/24).

Docs (4): `SESSION11-PLAN.md`, `MODEL-FABRIC-INFERENCE.md` (399),
`BACKENDS.md` (342), `SESSION11-REPORT.md` (this file).

No second runtime was created: the Native Model Runtime (PR #29) remains
canonical and is only *adapted to* and *gated by* the fabric. A32/A33 were not
weakened — A33 is now enforced on the inference path as well.

## Tests run (actual tree, Python 3.11.2, pytest 8.3.5)

`pytest -q -p no:randomly` → **2802 passed, 6 skipped** in 477 s, zero failures.

| Suite | Tests | What it proves |
|---|---|---|
| `test_inference_identity_backends` | 18 | identity transitions, CONFIGURED↛READY, spoofing, backend status derivation, registry closure |
| `test_inference_residency_streams` | 30 | single-flight loads, refcounts, eviction order, in-use protection, refusal codes, stream bounds, overflow, cancellation, snapshots |
| `test_inference_fabric_routing` | 29 | capability/policy/verification/cost/latency/resource filters, explainability, ladder tiers, honest terminal states |
| `test_inference_reference_engine` | 22 | artifact creation, discovery, real generation/streaming, tamper detection, no-download invariants |
| `test_inference_remote_provider` | 39 | hardened requester (methods, redirects, bounds, content type, pinning, audit redaction), provider config/credentials, adapter honesty, fabric-level A33/network/cost refusals |
| `test_inference_server_api` | 28 | routes, strict schemas, scopes, bounds, cursor replay, cancellation, disabled-by-default, no command endpoint |
| `test_inference_cli` | 33 | `forge models`/`forge infer` end to end, provenance, `--explain`, `--strict-neural`, g560 refusal vs server delegation, tamper via CLI |
| `test_inference_live` | 21 (+3 opt-in) | real uvicorn server over HTTP: typed client, CLI thin client, loopback Ollama doubles, non-loopback SSRF guard, network-off refusal |

Labels are enforced by naming inside the tests: `REAL_INFERENCE_TEST` (a real
backend produced the text — the reference engine, and the live cases),
`MOCK_BACKEND_TEST` (scripted or loopback doubles — never presented as real
inference), `DETERMINISTIC_TEST` (the non-neural rung). The three real-provider
cases are opt-in: `FORGE_REAL_INFERENCE=1` plus a reachable Ollama
(`/v1`-compatible endpoint) or `FORGE_REMOTE_<ID>_*` configuration; offline CI
skips them and stays fully functional.

Regression surface: `test_ssrf.py`, `test_research_secure_engine.py`,
`test_g560_thin_client.py`, `test_python38_compat.py`, `test_a81_*`,
`test_server_*` and the Session-10 scheduler/governor suites all pass unchanged.

## Backends actually exercised

| Backend | How it was exercised | Label |
|---|---|---|
| `reference` (first-party engine) | real artifact, real fingerprint verification, real forward pass, streaming, cancellation, tamper, CLI + server + HTTP | REAL_INFERENCE_TEST |
| `native` | runtime registration, discovery of artifacts, residency, governor refusals; no GGUF/safetensors weights exist in the sandbox | mixed (real code path, no real weights) |
| `ollama` | loopback HTTP double (`/api/tags`, `/api/generate`, NDJSON streaming), non-loopback refusal, network-off refusal, fabric delegation | MOCK_BACKEND_TEST (+ opt-in real) |
| `llama_cpp` | adapter registration, status/health honesty; no server binary in the sandbox | MOCK_BACKEND_TEST |
| `forge` / custom runtime backends | adapter exposure with own locality, custom backend registration | MOCK_BACKEND_TEST |
| `remote-<provider>` | loopback OpenAI-compatible double over real HTTP: discovery, generation, 401, malformed, spoofed model, empty completion, timeout, stream bound, redirect refusal, SSRF/policy refusals | MOCK_BACKEND_TEST (+ opt-in real) |
| `deterministic` rung | refusals, labelling, `neural=False`, `--strict-neural` exit codes | DETERMINISTIC_TEST |

No Ollama, llama.cpp or third-party provider binary exists in this sandbox and
no weights can be downloaded, so "real inference" offline means the reference
engine; everything else is a double or opt-in. That is stated in the tests
themselves, not only here.

## Security findings (this session's audit, §26)

1. **Audit records could carry a credential** (fixed). `ssrf._audit()` passed
   the raw URL and reason to the sink, so a URL with embedded userinfo
   (`https://user:key@host/`) or a redirect `Location`/`Authorization` echo
   could be persisted. Redaction is now central (`_redact_audit_text()`):
   userinfo is stripped and `Bearer`/`Basic`/`api_key=`/`Authorization:`/
   `password` values are masked before any sink sees them. Covered by
   `test_audit_records_never_carry_a_credential` and
   `test_audit_sees_a_bearer_token_only_redacted`.
2. **`max_stream_chars` bounded the chunk size, not the stream** (fixed). A
   remote provider's oversized completion was replayed in full. The bound now
   caps total replayed characters and the cut is reported
   (`truncated`, `provider_chars`, `finish_reason="length"`) instead of being
   presented as a complete answer.
3. **`--auto-verify` was a dead flag** (fixed). Selection refuses an unverified
   model, and verification only ran *after* selection, so the flag could never
   take effect: a freshly discovered model was refused as `unverified` no matter
   what the operator asked for. `InferenceFabric._auto_prepare()` now verifies
   (bounded: named model, or ≤ `AUTO_VERIFY_MAX`=4 candidates) **before**
   routing, records an `auto_verify` observation, and **skips non-local
   candidates when the request forbids outbound traffic**
   (`network_policy="off"` or a declared `secret`/`confidential`
   classification) so Forge does not probe a provider it is about to be refused
   by. The probe carries the verifier's prompt only — never request content.
4. **Policy denials could be masked by verification denials** (fixed). Because
   verification was checked before A33/network policy, a SECRET request aimed at
   an unverified remote provider reported `unverified`, which invites an
   operator to "just verify it". Absolute policy is now evaluated first and a
   verification refusal (which implies policy permitted that candidate) is the
   headline state. Both directions are tested.
5. **Two policies for one request** (fixed). The remote adapter built its own
   `FetchPolicy` for sending while the pre-flight check used
   `config.fetch_policy()`, so admission and transmission could disagree. One
   object now serves both; `test_send_uses_the_same_policy_as_the_preflight_check`
   pins it.
6. **An operator-configured local endpoint was unreachable** (fixed). The
   adapter refused every private destination and every non-default port, so a
   documented `FORGE_REMOTE_<ID>_URL=http://127.0.0.1:8080/v1` with
   `ALLOW_HTTP=1` could never work, while adding no safety. The opt-in is now
   explicit and narrow: `local_endpoint` (loopback **and** `allow_http`)
   tolerates that exact host, and the configured port is allowed; the
   destination-IP policy still applies to everything else.
7. **Prompt injection is treated as data, not as a guarantee.** Model output is
   scanned for instruction overrides, role hijacks, shell commands, permission
   grants, credential requests, tool invocations and exfiltration URLs; flags
   are reported (`output_flags`) and never authorize an action. Nothing
   executes model text, and the server has no command endpoint at all.
8. **Credentials.** No credential appears in logs, events, exceptions, audit
   records, stream summaries or test artifacts: `to_dict()` reports
   `api_key_set`; error text passes through `redact_text()` and a 500-char
   bound; the client's `AUTH_REQUIRED`/401 path asserts the token is absent from
   `str()`/`repr()`; the double's recorded request bodies assert the key travels
   only in the `Authorization` header.
9. **G560 stays a thin client.** Under the `g560` profile, local model loading
   is denied by the device profile (`resource_denied`, reason recorded on the
   candidate's own resource verdict) and the CLI/server path is delegation only;
   verified by CLI, fabric and live-server tests.
10. Pre-existing and unchanged: the runtime's secret redaction, the research
    engine's SSRF bounds, the server's strict schemas and constant-time token
    comparison, and `reject_execution_vectors()`.

## Performance (measured in this sandbox, reference engine, single process)

| Operation | Measured |
|---|---|
| Artifact creation (600,032 B) | 56 ms |
| Fabric build / discovery (1 model) | 4.3 ms / 1.1 ms |
| Verification (fingerprint 0.8 ms + health 0.3 ms + probe) | 98–127 ms |
| Generate, 32 output tokens, model resident | 118–144 ms (p50 127 ms) |
| First use with `--auto-verify` (verify + generate) | 157 ms |
| Stream 512 chars | TTFT 11.2 ms, total 1.98 s, 513 events |
| HTTP: discover+verify / generate / stream 64 chars / status | 149.9 / 171.8 (server 164.1) / 365.0 (server TTFT 15.9) / 5.8 ms |
| Residency after the run | 1 slot, 600,032 B resident, 1 load, 0 duplicate loads, 0 evictions, 0 refusals |
| Evidence summary | 6 requests, success 1.0, p50 127.5 ms, p95 1980.9 ms, 0 fallbacks/denials |

These numbers describe a **pure-Python character-level reference network**, not
a production model: the point is that the path is measured and honest
(provenance, TTFT, residency, evidence), not that it is fast. Streaming cost is
dominated by per-character Python delivery; the bounds (64 buffered events and
64 KiB per stream by default, a 128 KiB completion cap at the server) are what
keep it safe, not the throughput.

## Compatibility findings (Python 3.8 / Windows 7)

* `tests/test_python38_compat.py` (6 tests) walks **567 `.py` files** —
  including all **41** Session-11 files — and passes: 3.8 grammar, no
  PEP 585/604 constructs evaluated at runtime outside annotations, no
  `Path.walk`/3.9+ stdlib newcomers, `requires-python` consistent with the
  ceiling, dependency ceilings and the py38 lock still admitting 3.8.
* `python -m compileall forge/ tests/` — clean.
* `vermin --min-versions=3.8 forge/models forge/server/inference.py` — one
  finding: `remote.py` "str.decode member (requires 2.2, !3)". It is a **false
  positive**: the value is `FetchOutcome.body`, declared `bytes`
  (`body: bytes = b""`), so `.decode("utf-8", "replace")` is correct on 3.8 and
  is exercised by the remote-provider suite.
* Every new module uses `from __future__ import annotations` and
  `typing.Dict/List/Tuple/Optional/Iterator/Callable/Sequence` — no builtin
  generics or `X | Y` at runtime. No `dataclass(slots=/kw_only=)`, no
  positional-only markers, no `removeprefix`/`functools.cache`/`math.lcm`/
  `zoneinfo`/dict-`|` merge.
* Stdlib only: `threading`, `queue`, `time`, `hashlib`, `json`, `os`, `re`,
  `dataclasses`, `enum`, `contextlib`, `urllib.parse`, `http.client`, `ssl`,
  `socket`, `ipaddress`. No `subprocess`, no `multiprocessing`, no `AF_UNIX`,
  no `os.fork`/`fcntl`/`termios`/`chmod`/`symlink`, no `/tmp` hardcoding; paths
  use `os.path.join`. Clocks are `time.monotonic`/`perf_counter`.
* Windows 7: nothing in this session requires a newer API. G560 (Win7, 32-bit,
  2 GB) is *designed against*: the profile denies local model loading, caps
  resident model memory at zero, and routes to the server thin client; the
  reference artifact (600 KB) and the bounded streams/residency are what keep a
  2 GB machine from being asked to hold a model. Local inference under g560 is
  refused, not degraded.

## Remaining limitations (honest)

* **No production-grade neural model runs in this environment.** There are no
  GGUF/safetensors/ONNX weights, no Ollama and no llama.cpp binary, and nothing
  may be downloaded. Real inference offline means the first-party reference
  engine (character-level, `capabilities=()`, `trained=False`). Every claim of
  "real" in the tests is tied to that engine or to an opt-in live provider.
* **Ollama and llama.cpp adapters are proven against loopback doubles.** Their
  discovery output against a real installation is only covered by the opt-in
  live tests; the loopback double speaks the documented API shapes
  (`/api/tags`, `/api/generate`, NDJSON) and is labelled `MOCK_BACKEND_TEST`.
* **The runtime refuses all sockets when `allow_network=False` — including
  loopback.** That is the Session-10 posture and it was not weakened; tests that
  use a loopback double opt in explicitly, and the fabric adds its own
  loopback-only guard on top so `--allow-network` alone still cannot point
  Ollama at a remote host.
* **Remote streaming is a bounded chunked replay**, not incremental SSE: the
  stdlib client cannot stream a POST incrementally, so TTFT for a remote
  provider is the whole request's latency. The chunks are the provider's own
  text and the terminal chunk says how it was produced.
* **Cost is accounting, not metering.** The governor tracks spend reported by
  callers plus an exact per-token refusal; there is no billing integration and a
  provider that reports nothing spends nothing.
* **Verification TTL caching is per backend/process.** A long-lived server
  re-proves on TTL expiry or on demand; a fresh CLI process verifies again
  (~100 ms for the reference engine). Registry state is per-process, and the CLI
  says so when an answer (residency, evidence) cannot survive the invocation.
* **Auto-verify is bounded to 4 candidates** per request, so a registry with
  many unverified models may need an explicit `forge models verify`.
* **Data classification is pattern-based.** It catches credential-shaped material
  and raises the classification of a request, but it is not a DLP system;
  `SECRET` handling is a policy guarantee, detection is best effort.
* **Prompt-injection scanning is detection, not prevention.** Flags are
  informational; the guarantee is architectural (model output is data, no
  command endpoint, no execution of model text).
* **Branch naming.** The request asked for `arena/SESSION11-real-inference`;
  this Arena session is pinned to `arena/01a09a74-forge-ai`, so all work is on
  that branch and no other branch was created or pushed.
* **Base moved during the session.** `origin/main` advanced to `96e9161`
  (PRs #34/#35: hands-free voice command center and glass cockpit UI) after this
  branch was cut from `03b5a85`. The local clone is shallow, so the two tips have
  no visible common ancestor here and a local rebase was not attempted; the
  changed files do not overlap the cockpit/web work, and the PR is opened against
  `main` for review. **Not auto-merged**, per instruction.

## Commits (base `03b5a85` → HEAD)

```
efaaabe Session 11 (C): forge models / forge infer CLI and the live end-to-end suite
8e50ad8 Session 11 (B): typed server inference operations
885fe55 Session 11 (A): real inference fabric — identity, backends, routing, honest fallback
```

plus the docs commit that adds this report, `MODEL-FABRIC-INFERENCE.md`,
`BACKENDS.md` and `SESSION11-PLAN.md` (its SHA is the branch tip; see
`git log --oneline arena/01a09a74-forge-ai`).
