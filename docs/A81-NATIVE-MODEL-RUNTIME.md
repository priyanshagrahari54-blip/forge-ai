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
| `ModelRuntime` | The runtime: backend registry, model registry, generation, streaming, cancellation, health, resources, status. |
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

- **GGUF** — magic, version, tensor count, metadata-entry count (24-byte
  header).
- **safetensors** — the real JSON header: tensor count, declared format,
  declared architecture.
- **Ollama** — `/api/tags`: name, size, family, parameter size, quantization.

Anything the source does not say stays empty or zero. A file whose header does
not parse is reported with `header_ok: false` rather than being described
plausibly.

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
forge runtime load MODEL [--backend NAME] [--json]
forge runtime unload MODEL [--backend NAME] [--json]
```

Shared flags: `--backend`, `--config`, `--model-dir` (repeatable),
`--allow-network`, `--json` — accepted before or after the subcommand.

Exit codes: `status` / `models` / `backends` exit 0; `health` exits 0 only when
some backend can actually run inference; `load` / `unload` exit 1 on failure; a
configuration error exits 2.

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
  "history_size": 200
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
tests/test_a81_runtime_cli.py            forge runtime CLI
tests/test_a81_runtime_desktop.py        desktop backend integration
tests/test_a81_runtime_bridge.py         AI Engine -> runtime interface
tests/test_desktop_app_gui_stub.py       desktop runtime window and status pill
```

Every test double is explicitly labelled and only replays caller-supplied text,
so a passing test proves the *runtime* behaved — it never claims a model
produced something.

Per-file counts: registration 18, streaming 20, health 23, security 25,
backends 27, CLI 21, desktop 15, bridge 14 — 163 tests — plus 3 new desktop GUI
stub tests (166 new tests in total).

A81 result: **166 new tests; full suite 1989 passed, 3 skipped** (A80 baseline:
1823 passed, 3 skipped). `python -m compileall forge` and `git diff --check`
are clean.
