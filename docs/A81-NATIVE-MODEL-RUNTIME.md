# A81 — Forge Native Model Runtime

The Native Model Runtime is Forge's first-party **model execution layer**. It
is the component that can eventually replace Ollama as the thing that actually
runs models, while staying modular, lightweight, and honest about what it can
and cannot do.

```
forge/runtime/model_runtime.py
```

- **Runtime** — `forge.runtime.model_runtime`: model execution
  infrastructure. Discovery, metadata, load/unload, generation, streaming,
  health, bounded timeouts, cancellation, resource reporting.
- **Model** — the neural model: weights on disk, or a model served by an
  inference engine.
- **AI Engine** — `forge.core` / `forge.agents`: engineering orchestration
  (plan → code → test → review → accept).

The runtime is a dependency-free, stdlib-only module: it imports nothing from
`forge.core`, `forge.agents`, `forge.control`, or `forge.models`, and no
third-party package is required. It can be embedded, tested, and shipped on its
own.

## Three things that are not each other

This separation is the point of the module, so it is stated once, plainly.

| | What it is | What it is not |
|---|---|---|
| **Runtime** (`ModelRuntime`) | Model *execution infrastructure*. It finds models, reads their metadata, loads and unloads them, sends generation requests to a backend, bounds them in time, cancels them, and reports health and resources. | It is **not** a model. It never synthesises an answer. When no backend can run inference, it returns a failure — it does not invent output. |
| **Model** (`RuntimeModel`) | A neural model: a GGUF/safetensors/ONNX artifact on disk, or a model served by Ollama / llama.cpp. Metadata is read from the real file header or endpoint; unknown fields stay empty. | It is **not** infrastructure, and it is not described by guessing. The runtime never fabricates a context window or a parameter count. |
| **AI Engine** (`forge.core`, `forge.agents`) | Engineering orchestration: planning, coding, debugging, testing, review, acceptance. It *requests* inference through the runtime interface. | It is **not** the runtime, and it does not talk to model files or model servers directly. |

The dependency direction is strict and one-way:

```
AI Engine  ->  Model Fabric (routing/policy)  ->  ModelRuntime  ->  ModelBackend  ->  model
```

Nothing points the other way. The runtime does not know agents exist.

## Public interface

```python
from forge.runtime.model_runtime import (
    ModelRuntime, RuntimeConfig, RuntimeRequest, RuntimeResponse,
    RuntimeModel, RuntimeHealth, RuntimeResources, RuntimeChunk,
    RuntimeStream, CancellationToken, ModelBackend, InferenceAdapter,
    NativeBackend, OllamaBackend, LlamaCppBackend, ForgeInferenceBackend,
    ExternalClientBackend, create_backend,
)
```

| Type | Purpose |
|---|---|
| `ModelRuntime` | The runtime: backend registry, model registry, generation, streaming, cancellation, health, resources, status, `metrics()`, `history()`, `refusals()`. |
| `RuntimeRequest` | One inference request: prompt/system/task/context/instructions, model, explicit backend, bounds, ids. |
| `RuntimeResponse` | Structured result. Failures are values (`success=False`, `error_kind`), not exceptions. |
| `RuntimeModel` | Model metadata: id, backend, format, size, quantization, parameters, load state. |
| `RuntimeHealth` | Per-backend health plus observed counters (generations, failures, timeouts, cancellations). |
| `RuntimeResources` | Measured host resources plus runtime occupancy. |
| `RuntimeChunk` / `RuntimeStream` | Streaming: ordered chunks plus a final structured response. |
| `CancellationToken` | Cooperative cancellation shared by the runtime and a backend. |
| `ModelBackend` | The backend contract. Subclass it to add an execution technology. |
| `InferenceAdapter` | A real inference engine behind `NativeBackend`, supplied by the host. |

## Responsibilities

### Model discovery

`runtime.discover(backend="")` asks one backend — or every registered backend —
for its models. Discovery is explicit: nothing is discovered at construction,
and nothing is ever downloaded. One failing backend never hides the others;
per-backend errors are reported instead of aborting the sweep.

The native backend scans only the directories listed in
`RuntimeConfig.model_dirs`, at a bounded depth, for allowed extensions, never
following symlinks.

### Model metadata

Metadata is read, not assumed:

- **GGUF** — the magic, version, tensor count and entry count from the header,
  *plus the real key/value section*: `general.architecture`,
  `<arch>.context_length` (reported as `context_window`),
  `<arch>.embedding_length`, `<arch>.block_count`, `general.name`,
  `general.size_label` and `general.quantization_version`. Both header
  layouts are handled — v1 stores 32-bit counts at offset 16, v2/v3 store
  64-bit counts at offset 24, and misreading one as the other silently
  yields wrong numbers.
- **safetensors** — the real JSON header: tensor count, declared format,
  declared architecture, the dtypes present, and a `parameter_count` computed
  from the declared tensor shapes.
- **Ollama** — `/api/tags`: name, size, family, parameter size, quantization.

Parsing is bounded, because a metadata section is attacker-controllable:
`GGUF_METADATA_SCAN_BYTES` (4 MiB) caps how much is read,
`GGUF_MAX_KV_ENTRIES` / `GGUF_MAX_ARRAY_ELEMENTS` / `GGUF_MAX_STRING_BYTES`
cap the structures inside it. Arrays are counted and skipped, never
materialised — a tokenizer table of 150 000 entries costs a loop, not a
150 000-element list.

Anything the source does not say stays empty or zero. A file whose header does
not parse is reported with `header_ok: false` rather than being described
plausibly, and `metadata_truncated: true` means the section was *not* fully
read — including when parsing stopped at a safety cap. That flag is always
present, so "parsed cleanly" is distinguishable from "the parser never ran".

### Discovery identity

Two artifacts with the same filename in different directories are two models,
not one. Discovery names each artifact by its basename, and only widens the
name when a real collision exists (relative path, then root-qualified, then a
`#N` suffix), so no artifact can ever be silently dropped from the list. A
normal single-copy install keeps plain filenames.

### Missing model directories

A configured `model_dirs` entry that does not exist is reported, not skipped:
`NativeBackend.invalid_dirs()` lists them, `health()` names them in its detail
and reports the backend `degraded` rather than `ready`, and `resources()`
exposes `invalid_model_dirs`. Without this, a typo reads as "no models
installed" and sends the operator looking for a download problem.

### Loading and unloading

`runtime.load(model_id)` / `runtime.unload(model_id)` go through the backend's
loading abstraction, so callers get one lifecycle API regardless of the engine
underneath. For the native backend, loading admits a metadata-verified artifact
into the resident set; actual tensor loading belongs to the inference adapter.
For Ollama, weights are loaded by the server on first request — the runtime
never pulls a model, because that would be an unbounded silent download.

### Generation

```python
response = runtime.generate(RuntimeRequest(
    prompt="implement CSV export",
    model="native:tiny-q4.gguf",
    backend="native",           # explicit; empty means the configured default
    task="add CSV export",
    context=repo_context,
    max_output_tokens=1024,
    timeout=90.0,
))
if not response.success:
    handle(response.error_kind)  # timeout | cancelled | unavailable | ...
```

`generate()` never raises for operational failures. The result is a
`RuntimeResponse` with `success=False` and an `error_kind`.

### Streaming generation

```python
stream = runtime.stream(RuntimeRequest(prompt="...", backend="native"))
for chunk in stream:
    print(chunk.text, end="", flush=True)
final = stream.response          # structured result, always available after
```

Nothing is sent to the backend until the stream is iterated. Chunks arrive from
a daemon producer thread through a bounded queue, so the consumer enforces both
the overall deadline and a per-chunk timeout — even against a backend that
ignores its cancellation token. Iteration raises `RuntimeTimeoutError`,
`RuntimeCancelledError`, or a wrapped, redacted backend error; `stream.response`
is populated either way, so a mid-stream failure can never be mistaken for a
clean end of stream. Token counts are only reported when the backend genuinely
reports them; the runtime never estimates them.

### Health checks

`runtime.health(backend="", probe=True)` returns a `RuntimeHealth` per backend
combining a probe with observed counters. States are `ready`, `degraded`,
`unavailable`, or `unknown`. A backend that can only discover artifacts is
**degraded, never ready** — the runtime does not claim it can run inference when
it cannot. A probe never raises: an unreachable backend is reported
`unavailable` with a redacted error.

### Bounded timeouts

Every request has a wall-clock bound: `RuntimeConfig.timeout_seconds` by
default (120s), overridable per request, always clamped to
`max_timeout_seconds` (hard ceiling 1800s). Streaming additionally enforces
`chunk_timeout_seconds` between chunks. On expiry the runtime stops waiting,
cancels the token, and reports a timeout. The abandoned worker is a daemon
thread, so it can never block shutdown.

### Cancellation

```python
runtime.cancel(request_id)     # one request
runtime.cancel_all()           # everything in flight
runtime.close()                # cancel all + refuse new work
```

Cancellation is a first-class outcome, not a timeout in disguise: a request the
caller cancelled is reported as `error_kind="cancelled"`, `cancelled=True`,
`finish_reason="cancelled"`, with any partial text preserved and flagged
`metadata["partial"]`. The wait for a backend worker is sliced, so an explicit
cancel is observed promptly instead of after the full timeout.

### Resource reporting

`runtime.resources()` returns measured host state — CPU count, total and
available memory, platform, Python version — plus runtime occupancy: models
known, models loaded, requests in flight, backend count, and per-backend
extras. Accelerators are reported only when a backend reports one; they are
never invented. Values that cannot be measured on a platform stay `0`.

## Pluggable backends

| Backend | Kind | Network | Status |
|---|---|---|---|
| `native` | first-party | no | Discovery and metadata implemented. Inference requires an `InferenceAdapter` supplied by the host. |
| `ollama` | local server | yes (explicit) | Implemented over the standard library: `/api/tags`, `/api/generate`, streaming NDJSON. |
| `llama_cpp` | future | no | Inert until a client is explicitly provided. `llama_cpp` is never imported. |
| `forge` | future | yes (explicit) | Custom Forge inference backend. Inert until a client is explicitly provided. |

Adding a backend is either:

```python
from forge.runtime.model_runtime import ModelBackend, RuntimeModel, RuntimeResponse

class MyBackend(ModelBackend):
    name = "my-backend"
    kind = "custom"

    def available(self):
        return (True, "present")

    def list_models(self):
        return [RuntimeModel(model_id="my-backend:m1", name="m1",
                             backend="my-backend")]

    def generate(self, request, token=None):
        ...  # real inference; never invented output

runtime.register_backend(MyBackend())
```

…or, for a library/server the runtime must not import itself:

```python
runtime.register_backend(create_backend("llama_cpp", client=my_client))
```

## Using it from the AI Engine

The Model Fabric is unchanged. A runtime-backed provider is an explicit,
opt-in registration:

```python
from forge.models import ModelFabric
from forge.models.runtime_bridge import attach_runtime
from forge.runtime.model_runtime import ModelRuntime

runtime = ModelRuntime.from_defaults()
fabric = ModelFabric.from_defaults()
attach_runtime(fabric, runtime, backend="native", name="runtime")
```

`RuntimeProvider` translates the fabric's provider call into a
`RuntimeRequest` and back, so capability routing, policy filtering, failover,
health feedback, and telemetry all keep working. A runtime failure raises like
every other provider failure, so the existing failover chain handles it — and
the runtime's honest "I cannot infer" is never converted into text.

## CLI

```bash
forge runtime                          # status summary (same as: status)
forge runtime status [--probe] [--json]
forge runtime models [--model-dir DIR] [--no-discover] [--json]
forge runtime health [--offline] [--json]
forge runtime backends [--backend NAME] [--json]
forge runtime metrics [--backend NAME] [--json]
forge runtime test [--json]
forge runtime load MODEL [--backend NAME] [--json]
forge runtime unload MODEL [--backend NAME] [--json]
```

Shared flags: `--backend`, `--config`, `--model-dir` (repeatable),
`--allow-network`, `--json` — accepted before or after the subcommand.

Exit codes: `status` / `models` / `backends` / `metrics` exit 0; `health` exits
0 only when some backend can actually run inference; `test` exits 0 only when
every contract check passes; `load` / `unload` exit 1 on failure; a
configuration error exits 2.

### `forge runtime metrics`

Windowed latency and reliability figures computed from the recorded outcome
history: `success_rate`, p50/p95/p99 and min/max latency, a breakdown of
failure kinds, retry counts, and the same per backend. Percentiles come from
observed latencies — nothing is modelled or smoothed — and an empty window
reports `null` rather than a fabricated `0.0` that would read as "instant".
Lifetime counters are reported separately and labelled as such, because they
survive history truncation and can exceed the windowed figures.

Refusals are reported apart from `requests`. A request turned away before it
reached a backend (unknown backend name, duplicate in-flight request id, call
after `close`) is logged in `runtime.refusals()`, not in `history()`: history
and the per-backend counters describe generations that *did* reach a backend,
so folding refusals in would conjure counter entries for backends that do not
exist. They are separate, but they are not dropped.

### `forge runtime test`

An end-to-end self-check of the runtime's own guarantees. It exercises the
real code paths — generation, structured failure reporting, protocol-violation
classification, secret redaction, the timeout bound, cancellation, duplicate
request ids, streaming, backend-selection strictness, timeout validation,
metrics, and in-flight bookkeeping — against an in-process loopback backend.

It is explicit about what that does and does not prove: the checks verify the
*execution layer*, not that a real neural model is installed or that real
inference works, and the command reports the state of every real backend
separately. A backend that cannot serve is reported as such rather than being
papered over with a synthetic success. The check plants a key-shaped string in
an error message and asserts it comes back redacted.

## Configuration

`.forge/runtime.json` (or `.forge/runtime.yaml`), layered over `FORGE_RUNTIME_*`
environment variables. No secret material is stored or serialised here.

```json
{
  "default_backend": "native",
  "backends": ["native", "ollama"],
  "allow_network": false,
  "ollama_url": "http://127.0.0.1:11434",
  "ollama_model": "llama3.2",
  "model_dirs": ["/models"],
  "timeout_seconds": 120,
  "max_timeout_seconds": 1800,
  "chunk_timeout_seconds": 60,
  "health_timeout_seconds": 5,
  "history_size": 200,
  "retries": 0,
  "retry_backoff_seconds": 0.25,
  "max_resident_bytes": 0
}
```

| Variable | Meaning |
|---|---|
| `FORGE_RUNTIME_BACKEND` | Default backend |
| `FORGE_RUNTIME_BACKENDS` | Comma-separated built-ins to construct |
| `FORGE_RUNTIME_ALLOW_NETWORK` | `1`/`true` to permit the configured endpoint |
| `FORGE_RUNTIME_OLLAMA_URL` / `OLLAMA_BASE_URL` / `OLLAMA_URL` | Ollama endpoint |
| `FORGE_RUNTIME_OLLAMA_MODEL` / `OLLAMA_MODEL` | Default Ollama model |
| `FORGE_RUNTIME_MODEL_DIRS` | `os.pathsep`-separated model directories |
| `FORGE_RUNTIME_TIMEOUT` / `FORGE_RUNTIME_MAX_TIMEOUT` | Timeout bounds |
| `FORGE_RUNTIME_CHUNK_TIMEOUT` / `FORGE_RUNTIME_HEALTH_TIMEOUT` | Per-chunk and health bounds |
| `FORGE_RUNTIME_HISTORY_SIZE` | Outcome history window size |
| `FORGE_RUNTIME_RETRIES` / `FORGE_RUNTIME_RETRY_BACKOFF` | Bounded same-backend retries |
| `FORGE_RUNTIME_MAX_RESIDENT_BYTES` | Native resident-size cap (`0` = unbounded) |

Configuration is parsed strictly. An explicit `0` is honoured, not replaced by
the default, so `history_size: 0` and `retries: 0` mean what they say. A value
that cannot be a number raises `ValueError` naming the value, and a
non-positive timeout is rejected rather than silently swapped for the default.

A config file that exists but cannot be read or parsed is an **error**, not a
fallback to defaults: a one-character typo would otherwise make the runtime
ignore the operator's configuration — including which backend they asked for —
while appearing to work. A *missing* file remains perfectly fine.

### Retries

`retries` adds bounded extra attempts on the **same backend** for transient
failures only. `RETRYABLE_ERROR_KINDS` is `backend_error`, `unavailable` and
`protocol`; cancellations, timeouts, security refusals, unknown models,
conflicts and capacity refusals are never retried, because retrying them only
burns the deadline. `NEVER_RETRY_ERRORS` excludes `RuntimeCapacityError` by
type even though its kind is `unavailable`: a full resident set will not clear
on its own inside the request budget.

Backoff is exponential from `retry_backoff_seconds`, capped at
`MAX_RETRY_BACKOFF_SECONDS` (8s), and every sleep is interruptible by
cancellation and by the request deadline. The whole retry sequence runs inside
the single configured `timeout`, so retries can never outlive their request.
`RuntimeRequest.retries` overrides the configured value per request. The
attempts actually used are reported in `response.metadata["attempts"]`, so a
response that needed three tries is never mistaken for a first-try success.

Retries never cross backends. Silent failover would hide which backend
actually served a response; choosing another backend is a routing decision
that belongs to the caller.

### Resident memory cap

`max_resident_bytes` bounds the total declared size of artifacts the native
backend will admit. Exceeding it raises `RuntimeCapacityError` naming both the
projected size and the cap, and the model is not loaded. `0` means unbounded.
This is a declared-size budget — the runtime does not run tensors, so it
cannot measure actual footprint; the cap is honest about bounding what it can
see.

## Security

- **No secrets in logs.** Prompts, repository context, and completions are
  never logged or recorded. `RuntimeRequest.to_dict()` and
  `RuntimeResponse.to_dict()` carry sizes and identifiers only. Every string
  that leaves the module — including backend exception messages — passes
  `redact_text()`, and raw backend exceptions are wrapped before they reach a
  caller.
- **No arbitrary executable loading.** No module is ever imported from a
  user-supplied string; `create_backend()` builds only from a fixed allowlist
  (`native`, `ollama`, `llama_cpp`, `forge`), and a custom backend must be
  constructed in code and registered. Model artifacts are restricted to a
  non-executable extension allowlist (`.gguf`, `.safetensors`, `.onnx`).
  Pickle-based checkpoints (`.bin`, `.pt`, `.pth`, `.ckpt`, `.pkl`,
  `.pickle`, `.joblib`) are refused outright because unpickling executes
  arbitrary code. Optional engines (`llama_cpp`, `torch`, `transformers`,
  `onnxruntime`) are never imported by the runtime.
- **No unrestricted filesystem access.** Discovery scans only the explicitly
  configured directories, at a bounded depth and file count, never following
  symlinks. Any load path outside those directories — including `..` escapes —
  raises `RuntimeSecurityError`.
- **No silent network access.** `allow_network` defaults to `False`. A backend
  that needs the network reports itself unavailable and refuses discovery,
  generation, and health probes until network access is explicitly enabled.
  URLs with embedded credentials and non-HTTP schemes are refused.
- **Explicit provider/backend selection.** Backends are registered by the host
  process and selected by name per request. There is no implicit failover from
  one backend to another: an unknown backend is a `not_found` failure, and
  choosing another backend is a routing decision that belongs to the caller.
- **Bounded everything.** Timeouts, chunk waits, cancellation grace, discovery
  depth and file counts, and header read sizes are all bounded.

## Windows 7 / Python 3.8

The module targets Python 3.8 and uses the standard library only: no `match`,
no `removesuffix`, no `functools.cache`, no builtin generics outside string
annotations (`from __future__ import annotations` throughout), no
`dataclass(slots=)`. Windows memory reporting uses `ctypes`
`GlobalMemoryStatusEx` behind a guard, with `os.sysconf` preferred on POSIX and
a safe `(0, 0)` fallback everywhere. No new mandatory dependency is introduced.

## Testing

```
tests/helpers_a81.py                     shared test doubles (explicit, labeled)
tests/test_a81_runtime_registration.py   registration, routing, backend isolation
tests/test_a81_runtime_streaming.py      generation, streaming, timeouts, cancellation
tests/test_a81_runtime_health.py         health, resources, status, failures, redaction
tests/test_a81_runtime_security.py       the five security invariants
tests/test_a81_runtime_backends.py       native / Ollama / llama.cpp / custom backends
tests/test_a81_runtime_hardening.py      regression tests for every audited defect
tests/test_a81_runtime_cli.py            forge runtime CLI
tests/test_a81_runtime_desktop.py        desktop backend integration
tests/test_a81_runtime_bridge.py         AI Engine -> runtime interface
tests/test_desktop_app_gui_stub.py       desktop runtime window and status pill
```

Every test double is explicitly labelled and only replays caller-supplied text,
so a passing test proves the *runtime* behaved — it never claims a model
produced something.

Per-file counts: registration 18, streaming 21, health 23, security 25,
backends 27, hardening 56, CLI 32, desktop 15, bridge 14 — plus 3 desktop GUI
stub tests.

A81 result: **234 runtime tests; full suite 2057 passed, 3 skipped** (A80
baseline: 1823 passed, 3 skipped). `python -m compileall forge` and
`git diff --check` are clean, and the Python 3.8 compatibility gate passes.

## Hardening record

The defects below were found by auditing the shipped runtime and are each
covered by a regression test in `test_a81_runtime_hardening.py`, so a
regression fails loudly instead of quietly restoring the old behaviour.

**Correctness and honesty**

| Defect | Fix |
|---|---|
| A stream that failed part-way left its producer thread spinning forever | The producer observes an internal stop event set by the consumer's cleanup. |
| `clamp_timeout(nan)` returned 1ms, making every request time out instantly; `-5`/`0`/`inf` were silently swapped for defaults | Non-finite and non-positive timeouts raise `ValueError` naming the value. |
| Two artifacts with the same basename collapsed into one, silently dropping a model | Collision-aware naming; names widen only when a real collision exists. |
| Redaction scanned unbounded input — a 5 MB message cost 0.25s of CPU | Input is truncated to `MAX_ERROR_INPUT_CHARS` *before* the regexes run. |
| `RuntimeProvider.stream()` leaked `ModelRuntimeError` where the fabric expects `RuntimeError` | Stream acquisition is inside the wrapping `try`. |
| A backend returning a non-iterable from `stream()` was reported as a generic backend error | Classified as `BackendProtocolError` / `protocol`. |
| Loading a model with no artifact path blamed the model_dirs containment check | Reports that the model carries no artifact location. |
| Explicit zeros in configuration were swallowed (`history_size: 0` became 200) | `_pick()` preserves legitimate falsy values; non-numbers are rejected. |
| An Ollama connection reset mid-stream escaped as a raw `OSError` | Normalised to `BackendUnavailableError`, matching `generate()`. |
| Cancellation parked the consumer for the whole `chunk_timeout` (60s), then misreported it as a timeout | The queue wait is sliced so the token is checked every 20ms. |
| Text a backend produced at the moment of cancellation was discarded | Real output is preserved, flagged `partial`, outcome still `cancelled`. |
| A broken config file was silently ignored, so the runtime used the wrong backend | Unreadable/unparsable/non-mapping config raises `ValueError` naming the file. |
| Missing `model_dirs` entries vanished, reading as "no models installed" | Surfaced through `invalid_dirs()`, `health()`, `resources()`. |
| GGUF metadata was never read: `context_window` stayed 0 despite `llama.context_length=131072` | Real key/value parser; GGUF v1 and v2/v3 headers both handled. |
| GGUF parsing stopped at a safety cap but reported a clean parse | `metadata_truncated` is always present and true whenever the section was not fully read. |
| A capacity refusal was retried, because its kind is `unavailable` | `NEVER_RETRY_ERRORS` excludes it by type. |

**Added capability**

- GGUF key/value parsing: `context_window`, `architecture`,
  `embedding_length`, `block_count`, `declared_name`, `size_label`,
  `quantization`, and up to 32 `general.*` keys.
- safetensors `parameter_count` computed from declared shapes, plus `dtypes`.
- Bounded same-backend retries with interruptible exponential backoff.
- `ModelRuntime.metrics()`: p50/p95/p99, success rate, failure-kind breakdown,
  retry counts, per backend — plus `forge runtime metrics`.
- `max_resident_bytes` enforced on native load, raising `RuntimeCapacityError`.
- `runtime.refusals()`: pre-backend refusals, visible without polluting the
  per-backend counters with backends that do not exist.
- `forge runtime test`: an honest end-to-end self-check of the runtime's
  guarantees.
