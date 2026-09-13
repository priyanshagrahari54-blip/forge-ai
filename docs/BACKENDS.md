# Backends — the inference adapter reference

A **backend** is the component that actually produces tokens. Everything above
it (routing, policy, residency, streaming, evidence) is backend-agnostic: it
talks to one protocol, so a new backend does not need a new router.

There are two layers, and the split is deliberate:

```
forge/models/backends.py          the fabric-level Backend protocol + adapters
forge/runtime/model_runtime.py    the canonical runtime ModelBackend protocol
forge/models/reference_engine.py  first-party local reference backend (runtime)
forge/models/remote.py            remote HTTP provider backend (runtime)
```

```
InferenceFabric -> BackendRegistry -> Backend (fabric adapter)
                                        -> ModelRuntime -> ModelBackend -> model
```

The **fabric `Backend`** is what routing, verification and residency talk to.
The **runtime `ModelBackend`** is what executes. `RuntimeBackendAdapter` is the
canonical bridge: every operation goes through the Native Model Runtime, which
stays the one execution layer (Session 10, `docs/A81-NATIVE-MODEL-RUNTIME.md`).

## 1. The fabric protocol

```python
class Backend:
    backend_id: str        # unique in one registry; must not contain ":"
    kind: str              # native | ollama | llama_cpp | forge | remote | custom
    description: str
    local: bool            # locality — feeds A33 data policy
    requires_network: bool
    free: bool             # cost posture — feeds free-first routing

    def validate(self) -> None: ...
    def discover(self) -> List[ModelIdentity]: ...
    def health(self, probe: bool = True) -> BackendStatus: ...
    def load(self, model_id, *, token=None) -> ModelIdentity: ...
    def unload(self, model_id) -> bool: ...
    def generate(self, request, *, token=None) -> Any: ...
    def stream(self, request, *, token=None) -> Iterator[Any]: ...
    def cancel(self, request_id, *, reason="cancelled") -> bool: ...
    def resource_requirements(self, model_id="") -> ResourceRequirements: ...
    def in_flight(self) -> List[Dict[str, Any]]: ...
```

The defaults are honest: an unimplemented `discover()` returns nothing,
`health()` reports `configured=False, reachable=False, verified=False,
detail="not implemented"`, and `generate()`/`stream()` raise
`BackendNotReadyError`. A backend cannot fake a result by forgetting to
implement one.

`validate()` is called on registration: it requires a `backend_id` without `:`
(that character separates backend from model in model ids) and checks that all
eight operations are callable.

### BackendStatus — four separate answers

```python
configured   # the operator asked for it / an artifact really exists
reachable    # a real probe answered
verified     # a real generation proved the identity
denial       # a governor / network / policy refusal (kept distinct!)
ready = configured and reachable and verified and not denial   # recompute()
```

`ready` is **derived**, never asserted. A denial is reported in `denial` (with
`detail`/`error`), not flattened into `reachable=False`, so "why not?" has an
answer. The status also carries `latency_ms`, `models_available`,
`models_loaded`, `checked_at` and `extra`.

`BackendRegistry.statuses(probe=…)` wraps any backend exception into a
not-ready status whose `error` text is bounded (500 chars) and passed through
`redact_text()` — a backend that raises a credential in its message cannot leak
it into an API response.

### Errors → honest terminal states

Fabric-level: `BackendError`, `BackendNotReadyError`.
Runtime-level: `ModelRuntimeError` and its kinds — `BackendNotFoundError`,
`ModelNotFoundError`, `BackendUnavailableError`, `RuntimeCapacityError`,
`RuntimeSecurityError`, `RuntimeTimeoutError`, `RuntimeCancelledError`,
`BackendProtocolError`. `ReferenceBackendError` inherits from both
`BackendUnavailableError` and `BackendError` so either layer reports it
correctly.

`fallback.classify_error()` maps them onto terminal states; the flags win over
string matching, and the code map is closed:

| Signal | Terminal state |
|---|---|
| fenced / `STALE`, `FENCED`, `SUPERSEDED` | `stale` |
| cancelled / `CANCEL`, `ABANDONED`, `SHUTDOWN` | `cancelled` |
| timed out / `TIMEOUT`, `DEADLINE`, `WALL_TIME` | `timeout` |
| `RESOURCE`, `RESIDENCY`, `CAPACITY`, `MEMORY`, `OOM`, `CONCURRENCY`, `SCRATCH`, `COST_BUDGET` | `resource_denied` |
| `POLICY`, `NETWORK_POLICY`, `DATA_POLICY`, `SSRF`, `FORBIDDEN`, `UNAUTHORIZED`, `AUTH`, `CREDENTIAL`, `PERMISSION`, `APPROVAL` | `policy_denied` |
| `UNVERIFIED`, `VERIFICATION`, `FINGERPRINT`, `SPOOF` | `unverified` |
| `NOT_FOUND`, `NO_MODEL`, `NEEDS_MODEL`, `UNAVAILABLE` | `needs_model` |
| anything else | `failed` |

None of these may become text. A backend that cannot answer raises; the fabric
reports the refusal.

## 2. The adapters

Fabric adapters (all `RuntimeBackendAdapter` subclasses unless noted):

| Backend id | Class | `local` | `requires_network` | `free` | What it needs |
|---|---|---|---|---|---|
| `native` | `NativeLocalBackend` | yes | no | yes | artifacts in `--model-dir` / `FORGE_RUNTIME_MODEL_DIRS` |
| `ollama` | `OllamaCompatibleBackend` | yes | yes | yes | an Ollama server on **loopback** |
| `llama_cpp` | `LlamaCppCompatibleBackend` | yes | yes | yes | a llama.cpp-compatible server |
| `forge` | `ForgeCustomBackend` | yes | no | yes | first-party custom execution |
| `reference` | `RuntimeBackendAdapter` (custom) | yes | no | yes | `.forgeref` artifacts in `--reference-dir` |
| `remote-<id>` | `RemoteProviderBackend` (custom) | **no** | yes | **no** | `FORGE_REMOTE_<ID>_*` + `--allow-network` |
| `deterministic` | — (not a backend) | yes | no | yes | `LocalModelProvider` |

Notes that matter:

* `InferenceFabric.from_runtime()` registers only the backends the runtime
  actually has (`runtime.backends()`), so an unconfigured Ollama never appears
  as a phantom option; it stays registered but never becomes `ready`.
* A **custom runtime backend keeps its own locality**. The reference engine is
  local and offline and is wrapped with `local=True, free=True`; only a backend
  that really declares itself non-local is wrapped as `RemoteProviderBackend`
  (and then carries its declared `cost_per_token`, so free-first routing can
  tell "free" from "the operator did not say").
* `OllamaCompatibleBackend._guard()` refuses any non-loopback base URL with
  "not loopback". `allow_non_loopback` is never set by the fabric, so a remote
  Ollama is always denied — by policy, not by accident.
* `deterministic` is the last rung of the ladder, **not** a registered backend:
  `backend_id="deterministic"`, `model_id="deterministic:local-fallback"`,
  `neural=False`, and it never claims to be a model answer.

## 3. Configuring backends

Runtime level (Session 10 semantics, unchanged):

```
FORGE_RUNTIME_BACKENDS=native,ollama,forge      # closed set of builtin names
FORGE_RUNTIME_BACKEND=native                    # default backend
FORGE_RUNTIME_MODEL_DIRS=/models                # scan roots (never a download)
FORGE_RUNTIME_OLLAMA_URL=http://127.0.0.1:11434
FORGE_RUNTIME_OLLAMA_MODEL=…
FORGE_RUNTIME_ALLOW_NETWORK=0                   # 0 = no sockets at all
FORGE_RUNTIME_TIMEOUT / _HEALTH_TIMEOUT / _CHUNK_TIMEOUT / _MAX_TIMEOUT
FORGE_RUNTIME_MAX_RESIDENT_BYTES / _RETRIES / _RETRY_BACKOFF / _HISTORY_SIZE
```

Fabric level (CLI flags on `forge infer` / `forge models …`):

```
--reference-dir DIR   # .forgeref artifacts (repeatable)
--model-dir DIR       # native scan roots (repeatable); nothing is downloaded
--remote PROVIDER     # FORGE_REMOTE_<ID>_* provider (repeatable, needs --allow-network)
--ollama-url URL --allow-network --backend ID --auto-verify
--max-slots 2 --max-resident-mb 0 --idle-seconds 0 --resource-profile g560
```

`--allow-network` is off by default. Without it the runtime refuses **every**
socket — including loopback. That is the deliberate Session-10 posture: a
loopback test double must opt in explicitly, and nothing reaches the network by
accident. The fabric adds its own loopback guard on top, so `--allow-network`
alone still cannot make Ollama talk to a non-loopback host.

Remote providers:

```
FORGE_REMOTE_ACME_URL=https://api.acme.example/v1/chat/completions   # required
FORGE_REMOTE_ACME_MODEL=acme-1
FORGE_REMOTE_ACME_API_KEY_ENV=FORGE_REMOTE_ACME_API_KEY              # default
FORGE_REMOTE_ACME_API_KEY=…                                          # never logged
FORGE_REMOTE_ACME_TIMEOUT=60                                         # 0.5..600 s
FORGE_REMOTE_ACME_HOST_ALLOWLIST=api.acme.example
FORGE_REMOTE_ACME_CAPABILITIES=code,chat
FORGE_REMOTE_ACME_CONTEXT_WINDOW=32000
FORGE_REMOTE_ACME_COST_PER_TOKEN=0.000002
FORGE_REMOTE_ACME_ALLOW_HTTP=1        # honoured only for a loopback host
```

A missing `FORGE_REMOTE_<ID>_URL` is a loud configuration error, not a silent
skip. `RemoteProviderConfig.__post_init__` clamps every bound (timeout,
response bytes, stream chars) and **raises `RuntimeSecurityError`** if a
credential-bearing header (`authorization`, `proxy-authorization`, `cookie`,
`x-api-key`, `api-key`) appears in the config: keys are attached at send time
from `api_key`/`api_key_env` only.

## 4. Health and verification

| Backend | Reachability | Verification |
|---|---|---|
| `native`, `reference` | artifact present and parseable | sha256 fingerprint match + one bounded generation + reported-model match |
| `ollama`, `llama_cpp` | real health probe on loopback | health + one real completion + reported-model match |
| `remote-<id>` | `GET`/`POST` through the SSRF-hardened requester, TLS validated against the real hostname | health + one real completion + reported-model match |

`verification.ModelVerifier.verify()` is the single implementation; adapters
supply health and one probe generation. A reported model or fingerprint that
differs from the registered identity is **spoofing**, not a substitute
(`identity.assert_same_model`), and an empty completion fails the check.

Results are cached on the backend with a TTL (`backend.mark_verified(model_id,
ttl_seconds=…)`, cleared by `mark_unverified` on failure) so a long-lived
server does not re-probe on every request; `POST /models/verify` and
`forge models verify` re-prove on demand. A tampered artifact
(`FINGERPRINT_MISMATCH`) demotes the identity to `FAILED`/`unverified` and
generation is refused — the new bytes are never adopted silently.

## 5. Network, policy and resource posture

* **Locality drives A33.** `SECRET`/`CONFIDENTIAL` data may reach `local=True`
  backends only. A remote candidate is refused with `policy_denied` and the
  reason `secret data may not reach an external provider (deny)`. The
  classification is detected from the text as well as declared, so declaring
  "public" cannot launder a credential.
* **Network policy.** `off` (default) refuses every remote candidate;
  `server-only` permits the configured Forge Server endpoint
  (`RemoteProviderConfig.server_endpoint`); `explicit` permits allowlisted
  hosts. `network_policy=off` + a remote candidate = `policy_denied`.
* **Cost posture.** Remote identities are `free=False`; routing is free-first
  (a paid backend needs `allow_paid`), `max_cost_per_token` caps per-token
  price, and a remaining cost budget that cannot cover one token of a model
  refuses that model exactly (`resource_denied`) — never an estimate of output
  length.
* **SSRF.** Remote traffic goes through `forge.security.ssrf.request()`
  (new in Session 11): scheme → hostname → DNS → destination-IP → port chain,
  the connection pinned to the validated address (no DNS-rebinding window), TLS
  validated against the real hostname, **redirects refused outright**
  (`max_redirects=0`; a 3xx is a blocked outcome, never a retarget), body
  bounded (`max_response_bytes`, default cap 4 MiB) with a JSON/text content
  check, identity transfer-encoding only, a closed method set (`GET`, `POST`),
  and `Host`/`Connection`/`Accept-Encoding`/`Content-Length` always set by the
  requester so a caller header cannot defeat pinning or the size bound.
* **Credentials.** `api_key` is resolved at send time and never serialised —
  `to_dict()` reports `api_key_set` only. It never appears in logs, events,
  exceptions or audit records. URLs with embedded credentials
  (`https://user:pass@host/`) are refused outright by the adapter *and* by the
  requester, and every audit scope/reason passes through
  `ssrf._redact_audit_text()`, which strips URL userinfo and
  `Bearer`/`api_key=`/`Authorization:`-shaped values before a sink can persist
  them.
* **Operator-declared endpoints.** `RemoteProviderConfig.fetch_policy()` is the
  single source of truth for the pre-flight destination check *and* the send,
  so the two can never disagree. It honours exactly two operator-declared
  exceptions, both derived from the configured base URL: an explicit
  `allow_http` on a loopback host tolerates that host as a private destination,
  and an explicit non-default port is allowed. Everything else stays
  fail-closed.
* **Resource governor.** `resource_requirements()` reports model memory,
  minimum available RAM, concurrency slots, CPU threads, device profiles and
  whether the report was measured. The governor uses it to admit or deny; the
  `g560` profile (Win7 / 32-bit / 2 GB) refuses local model loading outright.
  Under g560 the supported paths are the deterministic rung and the server thin
  client (`forge infer --server URL --token T`).

## 6. The reference backend (real inference, no weights shipped)

`ReferenceLocalBackend` (runtime backend name `reference`) runs a tiny
character-level network written entirely by Forge. It exists so "real
inference" is testable offline without shipping or downloading a model.

```
forge models create-reference /tmp/ref/small.forgeref
forge models discover --reference-dir /tmp/ref
forge models verify   --reference-dir /tmp/ref
forge infer "hello"   --reference-dir /tmp/ref --capability ""
```

* Creation is explicit: `forge models create-reference`, or
  `FORGE_REFERENCE_CREATE=1`. Nothing is downloaded, ever.
* `ReferenceArtifactWriter.write()` returns `{path, size_bytes, fingerprint,
  config, trained: False, note}` — deterministic pseudo-random weights from a
  seed (default shape: 600,032 bytes). It never claims to be trained.
* It advertises `capabilities=()`, so **capability routing can never select
  it**; only an explicit `--capability ""`, `--model` or `--backend` path can.
  That keeps it out of any answer pretending to be agentic.
* Its output is labelled honestly: `neural=True` (a real forward pass produced
  it) with metadata stating it is a reference engine with no agentic
  capability.
* Discovery, loading, residency, streaming, cancellation and fingerprint
  verification all use the same code path as any other backend.
* With no artifact present it reports itself unavailable and generation fails —
  it never fabricates output.

## 7. Adding a backend

1. Implement the runtime `ModelBackend` (execution) and/or subclass the fabric
   `Backend` (or `RuntimeBackendAdapter`, which is preferred: it keeps the
   Native Runtime as the one execution layer). Implement all eight operations.
   Raise typed errors; never return text you did not receive.
2. Set `kind`, `local`, `requires_network`, `free`, `capabilities` and
   `description` honestly. Locality and cost feed A33 and free-first routing —
   getting them wrong is a security bug, not a cosmetic one.
3. Make `discover()` report only what is really there: ids, families, versions,
   context windows, parameter counts, quantization. Unknown fields stay empty.
4. Make `health()` a real probe, and report a governor/network refusal in
   `denial`, distinct from `reachable=False`.
5. Honour the cancellation token and the request bounds in
   `generate()`/`stream()`; emit monotonic chunk sequences and exactly one
   terminal chunk.
6. Report `resource_requirements()` so the governor can reason about memory,
   threads and slots (and say `measured=False` when it is a guess).
7. Register it: `runtime.register_backend(...)` (then `from_runtime` exposes
   it), or pass it to `build_inference_fabric`/`BackendRegistry.register`.
8. Test it with the labels used across the suite:
   * `MOCK_BACKEND_TEST` — a scripted or loopback double, never presented as
     real inference;
   * `REAL_INFERENCE_TEST` — a real backend produced the text (the reference
     engine qualifies; a stub does not);
   * `DETERMINISTIC_TEST` — the non-neural rung.

   Live provider tests are opt-in (`FORGE_REAL_INFERENCE=1`) and must skip
   cleanly offline; offline CI must stay fully functional.

Reviewer checklist: no fabricated output on failure, no silent model
substitution, no unbounded reads or streams, no credential leakage in
`health()`/`to_dict()`/exceptions, no network unless policy allows it, and no
capability claims the backend cannot honour.

## 8. Known limitations

* **No llama.cpp / Ollama binaries in CI.** Those adapters are exercised
  against loopback doubles (`MOCK_BACKEND_TEST`) and, when a real server is
  present, through the opt-in live tests. Their `discover()` output on a real
  installation is not asserted offline.
* **The reference engine is not a capable model.** It proves the path
  (verification, residency, streaming, cancellation, fencing, evidence) and is
  excluded from capability routing by design.
* **Remote streaming is a bounded chunked replay, not incremental SSE.** The
  stdlib HTTP client cannot stream a POST incrementally, so
  `RemoteHttpBackend.stream()` performs one bounded request and replays the
  provider's own text as chunks (`metadata.streamed_by="chunked-replay"`).
  `max_stream_chars` bounds the *total* replayed characters (default cap
  256 KiB); an oversized completion is cut at the bound and the cut is
  reported (`truncated=True`, `provider_chars`, `finish_reason="length"`),
  never presented as a complete answer.
* **Cost accounting is caller-reported.** The budget refuses exactly at the
  per-token price the operator configured; there is no billing integration.
* **No model download, no weight shipping.** A deployment needing a real neural
  model installs one out of band (native artifact / Ollama / llama.cpp) and
  registers it explicitly.
