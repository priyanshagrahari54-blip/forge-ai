"""Forge Native Model Runtime — first-party model execution infrastructure.

Three concepts are deliberately kept apart, and this module is only the
first of them:

``Runtime`` (this module)
    Model *execution infrastructure*: model discovery, model metadata,
    load/unload abstraction, generation, streaming generation, health
    checks, bounded timeouts, cancellation, and resource reporting.  It is
    plumbing — the thing that eventually replaces Ollama as Forge's model
    execution layer.

``Model``
    The neural model itself: weights on disk, or a model served by an
    inference engine.  The runtime is *not* a model and never synthesises
    an answer of its own.  When no backend can run inference, the runtime
    reports that failure honestly instead of inventing output.

``AI Engine``
    Engineering orchestration (``forge.core``, ``forge.agents``):
    planning, coding, testing, review, acceptance.  It *requests*
    inference through :class:`ModelRuntime` and owns everything above it.

The dependency direction is strict and one-way::

    AI Engine  ->  ModelRuntime  ->  ModelBackend  ->  model

Accordingly this module imports nothing from ``forge.core``,
``forge.agents``, ``forge.control``, or ``forge.models``, and it needs no
third-party package: it is stdlib-only, so it can be embedded, tested, and
shipped on its own.

Security model
--------------
* **No secrets in logs.** Prompts, repository context, and completions are
  never logged or recorded; every string that leaves this module passes
  :func:`redact_text`.
* **No arbitrary executable loading.** No module is ever imported from a
  user-supplied string, and model artifacts are restricted to a
  non-executable extension allowlist.  Pickle-based checkpoints
  (``.bin`` / ``.pt`` / ``.pth`` / ``.ckpt`` / ``.pkl``) are refused
  outright because unpickling executes arbitrary code.
* **No unrestricted filesystem access.** Discovery scans only the
  directories explicitly listed in :attr:`RuntimeConfig.model_dirs`, at a
  bounded depth, for allowed extensions.  Symlinks and ``..`` escapes are
  rejected.
* **No silent network access.** :attr:`RuntimeConfig.allow_network`
  defaults to ``False``; a backend that needs the network reports itself
  unavailable until network access is explicitly enabled.
* **Explicit backend selection.** Backends are registered by the host
  process and selected by name per request.  There is no implicit failover
  from one backend to another, and no data-driven import of third-party
  backends: a custom backend must be constructed and registered in code.

Python floor: 3.8 (Windows 7 reference target).  Stdlib only.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import queue
import re
import struct
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import (Any, Callable, Dict, Iterator, List, Mapping, Optional,
                    Sequence, Set, Tuple)
from uuid import uuid4

__all__ = [
    "ALLOWED_MODEL_EXTENSIONS",
    "BUILTIN_BACKENDS",
    "CANCEL_GRACE_SECONDS",
    "EXECUTABLE_EXTENSIONS",
    "GGUF_FILE_TYPES",
    "MAX_RETRY_BACKOFF_SECONDS",
    "NEVER_RETRY_ERRORS",
    "PICKLE_EXTENSIONS",
    "RETRYABLE_ERROR_KINDS",
    "RUNTIME_VERSION",
    "BackendKind",
    "BackendNotFoundError",
    "BackendProtocolError",
    "BackendUnavailableError",
    "CancelReason",
    "CancellationToken",
    "ErrorKind",
    "ExternalClientBackend",
    "FinishReason",
    "ForgeInferenceBackend",
    "InferenceAdapter",
    "LlamaCppBackend",
    "ModelBackend",
    "ModelNotFoundError",
    "ModelRuntime",
    "ModelRuntimeError",
    "NativeBackend",
    "OllamaBackend",
    "RuntimeBackendInfo",
    "RuntimeCancelledError",
    "RuntimeCapacityError",
    "RuntimeChunk",
    "RuntimeConfig",
    "RuntimeHealth",
    "RuntimeModel",
    "RuntimeRequest",
    "RuntimeResources",
    "RuntimeResponse",
    "RuntimeSecurityError",
    "RuntimeState",
    "RuntimeStream",
    "RuntimeTimeoutError",
    "classify_artifact",
    "create_backend",
    "create_default_runtime",
    "describe_artifact",
    "describe_gguf",
    "describe_safetensors",
    "redact",
    "redact_text",
    "system_resources",
]

#: Version of the runtime contract. Bumped on incompatible interface changes.
RUNTIME_VERSION = "1.0.0"

#: Bounded defaults. Everything here is overridable through
#: :class:`RuntimeConfig`; nothing is infinite.
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 1800.0
DEFAULT_CHUNK_TIMEOUT_SECONDS = 60.0
DEFAULT_HEALTH_TIMEOUT_SECONDS = 5.0
#: Extra seconds a worker gets to notice cancellation and land a result
#: before the runtime stops waiting for it and abandons the call. The worker
#: is a daemon thread, so an abandoned call can never block shutdown; the
#: bound only keeps cancellation responsive.
CANCEL_GRACE_SECONDS = 0.5
#: Most chunks recovered from a producer that finished during the grace
#: window. Bounded so a burst cannot turn cleanup into an unbounded copy.
_GRACE_DRAIN_CHUNKS = 4096
#: Largest header this module will read when parsing model metadata.
MAX_HEADER_BYTES = 8 * 1024 * 1024
#: Largest file discovery will even look at (8 GiB); a model artifact is big,
#: but this bounds a hostile directory of sparse files.
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024
#: How far into a GGUF file the metadata key/value section is scanned. The
#: tokenizer tables live in this region, so a small bound would stop before
#: reaching ``<arch>.context_length``; 4 MiB is a small fraction of any real
#: artifact and keeps the read bounded.
GGUF_METADATA_SCAN_BYTES = 4 * 1024 * 1024
#: Safety caps on GGUF metadata parsing.
GGUF_MAX_KV_ENTRIES = 4096
GGUF_MAX_ARRAY_ELEMENTS = 4 * 1024 * 1024
GGUF_MAX_STRING_BYTES = 64 * 1024
#: Longest error message inspected for secrets, and the longest kept. The
#: input is bounded *before* the regexes run so a huge or hostile message
#: cannot become a CPU sink.
MAX_ERROR_INPUT_CHARS = 8192
MAX_ERROR_CHARS = 500

logger = logging.getLogger("forge.runtime.model")


# ---------------------------------------------------------------------------
# Redaction (secrets never reach a log line, an error message, or telemetry)
# ---------------------------------------------------------------------------

REDACTED = "[REDACTED]"

_SECRET_PATTERNS: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"(?i)\b(sk|pk|rk|ak)-[a-z0-9_\-]{12,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd|pwd|"
        r"authorization|access[_-]?key|client[_-]?secret)\b"
        r"(\s*[:=]\s*)([^\s,;\"']+)"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b[basic]\s+[A-Za-z0-9+/=]{12,}"),
)


def redact_text(text: str) -> str:
    """Mask secret-looking values in a string. Never raises."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    masked = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            masked = pattern.sub(
                lambda match: match.group(1) + match.group(2) + REDACTED,
                masked)
        else:
            masked = pattern.sub(REDACTED, masked)
    return masked


def redact(value: Any) -> Any:
    """Recursively mask secret-looking values in a payload."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def _error_text(exc: BaseException) -> str:
    """A redacted, single-line, length-bounded description of an exception.

    The message is truncated *before* the redaction patterns run, so a
    multi-megabyte error string cannot turn a failure path into a CPU sink.
    Redaction still applies to everything that is kept.
    """
    message = str(exc) or exc.__class__.__name__
    if len(message) > MAX_ERROR_INPUT_CHARS:
        message = message[:MAX_ERROR_INPUT_CHARS] + "...[truncated]"
    return redact_text(" ".join(message.split()))[:MAX_ERROR_CHARS]


def _wrap_backend_error(exc: BaseException) -> BaseException:
    """A re-raisable, secret-free version of a backend exception.

    The runtime is a trust boundary: a raw backend exception is never
    propagated with its original message, because that message can contain
    anything the backend put in it (an endpoint with a key, an echoed
    prompt).  The runtime's own cancellation/timeout errors are already
    runtime-generated and pass through unchanged.
    """
    if isinstance(exc, (RuntimeCancelledError, RuntimeTimeoutError)):
        return exc
    safe = _error_text(exc)
    if isinstance(exc, ModelRuntimeError) and str(exc) == safe:
        return exc
    if isinstance(exc, ModelRuntimeError):
        try:
            wrapped: BaseException = type(exc)(safe)
        except Exception:
            wrapped = ModelRuntimeError(safe)
    else:
        wrapped = ModelRuntimeError(safe)
    wrapped.__cause__ = exc
    return wrapped


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RuntimeState(str, Enum):
    """Backend/runtime health states."""

    UNKNOWN = "unknown"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class BackendKind(str, Enum):
    """Which execution technology a backend wraps."""

    NATIVE = "native"
    OLLAMA = "ollama"
    LLAMA_CPP = "llama_cpp"
    FORGE = "forge"
    CUSTOM = "custom"


class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    ERROR = "error"


class ErrorKind(str, Enum):
    """Why a :class:`RuntimeResponse` failed. Empty string means success."""

    NONE = ""
    BACKEND = "backend_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"
    SECURITY = "security"
    PROTOCOL = "protocol"
    CONFLICT = "conflict"
    CLOSED = "closed"


#: Cancellation reasons recorded on a :class:`CancellationToken`.
class CancelReason(str, Enum):
    USER = "cancelled"
    TIMEOUT = "timeout"
    ABANDONED = "abandoned"
    SHUTDOWN = "shutdown"


#: Failure kinds worth trying again on the *same* backend. A transient
#: transport blip or a protocol hiccup may clear; a cancellation, a timeout,
#: a security refusal, an unknown model or a capacity refusal never will, and
#: retrying those only burns the deadline. Retries never cross backends:
#: silent failover would hide which backend actually served a response.
RETRYABLE_ERROR_KINDS = frozenset((
    ErrorKind.BACKEND.value,
    ErrorKind.UNAVAILABLE.value,
    ErrorKind.PROTOCOL.value,
))
#: Longest single backoff between retries, whatever the configured base is.
MAX_RETRY_BACKOFF_SECONDS = 8.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModelRuntimeError(Exception):
    """Base class for every runtime failure."""

    kind = ErrorKind.BACKEND


class BackendNotFoundError(ModelRuntimeError):
    """No backend is registered under the requested name."""

    kind = ErrorKind.NOT_FOUND


class ModelNotFoundError(ModelRuntimeError):
    """No model is registered under the requested id."""

    kind = ErrorKind.NOT_FOUND


class BackendUnavailableError(ModelRuntimeError):
    """The backend exists but cannot serve this request right now."""

    kind = ErrorKind.UNAVAILABLE


class RuntimeCapacityError(ModelRuntimeError):
    """A configured resource budget refused the operation."""

    kind = ErrorKind.UNAVAILABLE


#: Exception types that are never retried, whatever their ``kind`` says.
#: A capacity refusal is reported as ``unavailable`` because the backend
#: genuinely cannot serve right now — but the condition will not clear on
#: its own inside the request's budget, so retrying it only burns the
#: deadline. The type check runs before the kind check for exactly this
#: reason: kind describes how to *report* a failure, not whether retrying
#: could plausibly help.
NEVER_RETRY_ERRORS: Tuple[type, ...] = (RuntimeCapacityError,)


class RuntimeSecurityError(ModelRuntimeError):
    """A security constraint refused the operation."""

    kind = ErrorKind.SECURITY


class RuntimeTimeoutError(ModelRuntimeError):
    """A bounded timeout expired."""

    kind = ErrorKind.TIMEOUT


class RuntimeCancelledError(ModelRuntimeError):
    """The request was cancelled."""

    kind = ErrorKind.CANCELLED


class BackendProtocolError(ModelRuntimeError):
    """A backend violated the backend contract."""

    kind = ErrorKind.PROTOCOL


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class CancellationToken:
    """Cooperative cancellation shared by the runtime and a backend.

    Backends are expected to check :attr:`cancelled` at every point where
    they can stop cheaply (between streamed chunks, between polling reads).
    A backend that ignores the token is still bounded: the runtime stops
    waiting for it and reports the timeout honestly.
    """

    __slots__ = ("_event", "_reason", "_at")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""
        self._at = 0.0

    def cancel(self, reason: str = CancelReason.USER.value) -> bool:
        """Request cancellation. Idempotent; returns ``True`` on first call."""
        if self._event.is_set():
            return False
        self._reason = str(reason) or CancelReason.USER.value
        self._at = time.time()
        self._event.set()
        return True

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def cancelled_at(self) -> float:
        return self._at

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until cancelled or ``timeout`` elapses."""
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if not self.cancelled:
            return
        if self.reason == CancelReason.TIMEOUT.value:
            raise RuntimeTimeoutError("Runtime request timed out.")
        raise RuntimeCancelledError(
            "Runtime request cancelled ({0}).".format(self.reason))


# ---------------------------------------------------------------------------
# Request / response / model / health / resources
# ---------------------------------------------------------------------------


@dataclass
class RuntimeRequest:
    """One inference request addressed to a named backend.

    ``backend`` selects the execution backend explicitly.  An empty value
    means "the configured default backend" — never "whichever backend
    happens to answer".  Content fields are forwarded verbatim; nothing in
    this class is ever logged.
    """

    prompt: str = ""
    #: Model id (``"<backend>:<name>"``) or a bare model name.
    model: str = ""
    #: Explicit backend selection. Empty = configured default backend.
    backend: str = ""
    system: str = ""
    context: str = ""
    task: str = ""
    instructions: str = ""
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stop: Tuple[str, ...] = ()
    seed: Optional[int] = None
    #: Bounded wall-clock budget in seconds. ``None`` = the configured
    #: default; values are always clamped to ``RuntimeConfig`` bounds.
    timeout: Optional[float] = None
    #: Per-request override of ``RuntimeConfig.retries``: extra attempts on
    #: the *same* backend for retryable failures only. ``None`` = use the
    #: configured value. Retries never cross backends and never outlive the
    #: request ``timeout``.
    retries: Optional[int] = None
    request_id: str = field(default_factory=lambda: "rt-" + uuid4().hex[:16])
    trace_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id:
            self.request_id = "rt-" + uuid4().hex[:16]
        self.stop = tuple(self.stop or ())

    def compose_prompt(self) -> str:
        """Render the request as one labelled prompt.

        Backends whose API only accepts a single prompt use this so that
        task / instructions / repository context are preserved instead of
        being silently dropped at the backend boundary.
        """
        sections: List[Tuple[str, str]] = []
        if self.system and self.system.strip():
            sections.append(("SYSTEM", self.system.strip()))
        if self.task and self.task.strip():
            sections.append(("TASK", self.task.strip()))
        if self.prompt and self.prompt.strip():
            sections.append(("INSTRUCTIONS", self.prompt.strip()))
        if self.context and self.context.strip():
            sections.append(("REPOSITORY CONTEXT", self.context.strip()))
        if self.instructions and self.instructions.strip():
            sections.append(("CONSTRAINTS", self.instructions.strip()))
        if not sections:
            return ""
        return "\n\n".join(
            "{0}\n{1}".format(label, body) for label, body in sections)

    def to_dict(self) -> Mapping[str, Any]:
        """A loggable view: sizes and identifiers only, never content."""
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "backend": self.backend,
            "model": self.model,
            "prompt_chars": len(self.prompt or ""),
            "context_chars": len(self.context or ""),
            "task_chars": len(self.task or ""),
            "instructions_chars": len(self.instructions or ""),
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "timeout": self.timeout,
        }


@dataclass
class RuntimeResponse:
    """A structured, backend-agnostic inference result.

    Failures are values, not exceptions: ``success=False`` with an
    ``error`` and an ``error_kind`` so callers branch deterministically.
    A cancelled or timed-out request keeps any partial text in ``text``
    and says so through ``finish_reason``.
    """

    text: str = ""
    model: str = ""
    backend: str = ""
    success: bool = True
    error: str = ""
    error_kind: str = ErrorKind.NONE.value
    cancelled: bool = False
    timed_out: bool = False
    request_id: str = ""
    trace_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = FinishReason.STOP.value
    raw: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, error: str, *, kind: str = ErrorKind.BACKEND.value,
                backend: str = "", model: str = "", request_id: str = "",
                trace_id: str = "", latency_ms: float = 0.0,
                **kwargs: Any) -> "RuntimeResponse":
        return cls(
            text="", success=False, error=redact_text(error),
            error_kind=kind, backend=backend, model=model,
            request_id=request_id, trace_id=trace_id, latency_ms=latency_ms,
            finish_reason=FinishReason.ERROR.value, **kwargs)

    @property
    def ok(self) -> bool:
        return self.success

    def to_dict(self) -> Mapping[str, Any]:
        """A loggable view: never includes generated text or prompt content."""
        return {
            "success": self.success,
            "error": self.error,
            "error_kind": self.error_kind,
            "backend": self.backend,
            "model": self.model,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "text_chars": len(self.text or ""),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": round(float(self.latency_ms or 0.0), 3),
            "finish_reason": self.finish_reason,
            "cancelled": self.cancelled,
            "timed_out": self.timed_out,
            "metadata": redact(dict(self.metadata or {})),
        }


@dataclass
class RuntimeModel:
    """Metadata for one model known to one backend.

    ``model_id`` is the runtime-wide key: ``"<backend>:<name>"``.  Every
    field is either read from the artifact/endpoint or left empty — this
    class never guesses a context window or a parameter count.
    """

    model_id: str = ""
    name: str = ""
    backend: str = ""
    #: ``text`` | ``vision`` | ``embedding`` | ``unknown``
    kind: str = "unknown"
    capabilities: Tuple[str, ...] = ()
    #: 0 means "not reported by the backend" (never a fabricated default).
    context_window: int = 0
    max_output_tokens: int = 0
    size_bytes: int = 0
    #: ``gguf`` | ``safetensors`` | ``onnx`` | ``ollama`` | ``unknown``
    format: str = "unknown"
    quantization: str = ""
    parameters: str = ""
    local: bool = True
    loaded: bool = False
    #: Only ever a path inside an explicitly allowed model directory.
    path: str = ""
    discovered_at: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.model_id and not self.backend:
            self.backend = self.model_id.split(":", 1)[0]
        if self.model_id and not self.name:
            self.name = self.model_id.split(":", 1)[-1]
        self.capabilities = tuple(self.capabilities or ())

    @staticmethod
    def make_id(backend: str, name: str) -> str:
        return "{0}:{1}".format(backend, name)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "backend": self.backend,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "size_bytes": self.size_bytes,
            "format": self.format,
            "quantization": self.quantization,
            "parameters": self.parameters,
            "local": self.local,
            "loaded": self.loaded,
            "path": self.path,
            "discovered_at": self.discovered_at,
            "metadata": redact(dict(self.metadata or {})),
        }


@dataclass
class RuntimeHealth:
    """Health of one backend, combining a probe with observed counters."""

    backend: str = ""
    kind: str = BackendKind.CUSTOM.value
    status: str = RuntimeState.UNKNOWN.value
    checked_at: float = 0.0
    latency_ms: float = 0.0
    error: str = ""
    detail: str = ""
    models_available: int = 0
    models_loaded: int = 0
    generations: int = 0
    failures: int = 0
    timeouts: int = 0
    cancellations: int = 0
    #: ``True`` only when a live probe actually ran this call.
    probed: bool = False
    network_allowed: bool = False
    requires_network: bool = False

    @property
    def ok(self) -> bool:
        return self.status == RuntimeState.READY.value

    @property
    def usable(self) -> bool:
        return self.status in (RuntimeState.READY.value,
                               RuntimeState.DEGRADED.value)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "backend": self.backend,
            "kind": self.kind,
            "status": self.status,
            "checked_at": self.checked_at,
            "latency_ms": round(float(self.latency_ms or 0.0), 3),
            "error": self.error,
            "detail": self.detail,
            "models_available": self.models_available,
            "models_loaded": self.models_loaded,
            "generations": self.generations,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "cancellations": self.cancellations,
            "probed": self.probed,
            "network_allowed": self.network_allowed,
            "requires_network": self.requires_network,
        }


@dataclass
class RuntimeResources:
    """Resource reporting: measured host state plus runtime occupancy.

    Values that cannot be measured on this platform stay ``0``/empty and
    are reported as unknown.  Accelerators are never invented: the list is
    empty unless a backend reports one.
    """

    cpu_count: int = 0
    memory_total_bytes: int = 0
    memory_available_bytes: int = 0
    platform: str = ""
    python_version: str = ""
    models_known: int = 0
    models_loaded: int = 0
    in_flight: int = 0
    backends: int = 0
    accelerators: Tuple[str, ...] = ()
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "cpu_count": self.cpu_count,
            "memory_total_bytes": self.memory_total_bytes,
            "memory_available_bytes": self.memory_available_bytes,
            "memory_total_mb": round(self.memory_total_bytes / 1048576.0, 1),
            "memory_available_mb": round(
                self.memory_available_bytes / 1048576.0, 1),
            "platform": self.platform,
            "python_version": self.python_version,
            "models_known": self.models_known,
            "models_loaded": self.models_loaded,
            "in_flight": self.in_flight,
            "backends": self.backends,
            "accelerators": list(self.accelerators),
            "extra": redact(dict(self.extra or {})),
        }


@dataclass(frozen=True)
class RuntimeBackendInfo:
    """Declarative description of a registered backend (no secrets)."""

    name: str
    kind: str = BackendKind.CUSTOM.value
    local: bool = True
    requires_network: bool = False
    available: bool = False
    detail: str = ""
    description: str = ""

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "local": self.local,
            "requires_network": self.requires_network,
            "available": self.available,
            "detail": self.detail,
            "description": self.description,
        }


@dataclass
class RuntimeChunk:
    """One streamed piece of output.

    Backends may yield plain ``str`` chunks (the common case) or a
    :class:`RuntimeChunk` when they can also report usage.  Token counts
    stay ``0`` unless the backend genuinely reports them; the runtime never
    estimates them.
    """

    text: str = ""
    index: int = 0
    done: bool = False
    request_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "index": self.index,
            "chars": len(self.text or ""),
            "done": self.done,
            "request_id": self.request_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "finish_reason": self.finish_reason,
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Built-in backends. ``create_backend`` builds *only* from this allowlist;
#: an arbitrary dotted path is never imported.
BUILTIN_BACKENDS: Tuple[str, ...] = ("native", "ollama", "llama_cpp", "forge")


@dataclass
class RuntimeConfig:
    """Declarative runtime configuration.

    Loaded from ``.forge/runtime.yaml`` / ``.forge/runtime.json`` and/or
    ``FORGE_RUNTIME_*`` environment variables.  No secret material is ever
    stored here or serialised by :meth:`to_dict`.
    """

    #: Backend used when a request does not name one. Must resolve to a
    #: registered backend at request time: either a built-in listed in
    #: ``backends`` or a custom backend registered in code.
    default_backend: str = "native"
    #: Built-in backends to construct. Explicit allowlist: an entry that is
    #: not in :data:`BUILTIN_BACKENDS` is rejected by :meth:`validate`.
    backends: Tuple[str, ...] = ("native",)
    #: Master switch for any network access. Default off.
    allow_network: bool = False
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    #: Directories the native backend may scan. Empty = no discovery.
    model_dirs: Tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_timeout_seconds: float = MAX_TIMEOUT_SECONDS
    chunk_timeout_seconds: float = DEFAULT_CHUNK_TIMEOUT_SECONDS
    health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS
    #: Number of outcomes retained for `ModelRuntime.history()`.
    history_size: int = 200
    #: Bounded retries for a *transient* backend failure. Retries always stay
    #: on the same backend: the runtime never fails over to another one,
    #: because choosing a backend is the caller's decision. Cancelled,
    #: timed-out, security, protocol, and not-found outcomes are never
    #: retried.
    retries: int = 0
    #: Base backoff between attempts; grows linearly and is interruptible by
    #: cancellation.
    retry_backoff_seconds: float = 0.25
    #: Upper bound on bytes the native backend may hold resident. ``0`` means
    #: unbounded. Loading past the budget raises ``RuntimeCapacityError``.
    max_resident_bytes: int = 0

    def validate(self) -> "RuntimeConfig":
        if not self.default_backend:
            raise ValueError("default_backend cannot be empty")
        if not self.backends:
            raise ValueError("at least one backend must be enabled")
        for name in self.backends:
            if name not in BUILTIN_BACKENDS:
                raise ValueError(
                    "Unknown built-in backend {0!r}; registered backends "
                    "must come from {1}. Custom backends are registered in "
                    "code with ModelRuntime.register_backend().".format(
                        name, ", ".join(BUILTIN_BACKENDS)))
        # ``default_backend`` is intentionally not required to appear in
        # ``backends``: the latter lists the *built-ins to construct*, while
        # the default may also be a custom backend registered in code.
        # "Is the default actually registered?" is checked where the full
        # backend set is known (ModelRuntime.from_defaults) and again at
        # request time (select_backend), which is explicit either way.
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_timeout_seconds < self.timeout_seconds:
            raise ValueError(
                "max_timeout_seconds must be >= timeout_seconds")
        if self.max_timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise ValueError(
                "max_timeout_seconds cannot exceed {0}".format(
                    MAX_TIMEOUT_SECONDS))
        if self.chunk_timeout_seconds <= 0:
            raise ValueError("chunk_timeout_seconds must be positive")
        if self.health_timeout_seconds <= 0:
            raise ValueError("health_timeout_seconds must be positive")
        if self.history_size < 0:
            raise ValueError("history_size cannot be negative")
        for name in ("timeout_seconds", "max_timeout_seconds",
                     "chunk_timeout_seconds", "health_timeout_seconds",
                     "retry_backoff_seconds"):
            value = getattr(self, name)
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError("{0} must be a finite number".format(name))
        if self.retries < 0:
            raise ValueError("retries cannot be negative")
        if self.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds cannot be negative")
        if self.max_resident_bytes < 0:
            raise ValueError("max_resident_bytes cannot be negative")
        if "@" in self.ollama_url:
            raise ValueError(
                "ollama_url must not embed credentials (use no userinfo)")
        return self

    def clamp_timeout(self, timeout: Optional[float]) -> float:
        """Clamp a requested timeout into the configured bounds.

        A non-finite or non-positive request is a programming error, not
        something to silently reinterpret: ``nan`` in particular would
        otherwise clamp to 1ms and make every request time out instantly.
        """
        if timeout is None:
            requested = self.timeout_seconds
        else:
            requested = float(timeout)
            if requested != requested or requested in (float("inf"),
                                                       float("-inf")):
                raise ValueError(
                    "timeout must be a finite number of seconds, got "
                    "{0!r}".format(timeout))
            if requested <= 0:
                raise ValueError(
                    "timeout must be positive, got {0!r}".format(timeout))
        return max(0.001, min(requested, self.max_timeout_seconds))

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]] = None,
                  *, env: Optional[Mapping[str, str]] = None
                  ) -> "RuntimeConfig":
        data = dict(data or {})
        env = dict(os.environ if env is None else env)

        def _bool(raw: Any, default: bool = False) -> bool:
            if raw is None:
                return default
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

        def _tuple(raw: Any, sep: str = ",") -> Tuple[str, ...]:
            if raw is None:
                return ()
            if isinstance(raw, (list, tuple, set)):
                return tuple(str(item) for item in raw if str(item).strip())
            return tuple(part.strip() for part in str(raw).split(sep)
                         if part.strip())

        def _pick(*candidates: Any) -> Any:
            """First candidate that is present.

            Unlike ``a or b`` this preserves legitimate falsy values, so an
            explicit ``history_size: 0`` or ``retries: 0`` is honoured instead
            of being silently replaced by the default.
            """
            for value in candidates:
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                return value
            return None

        def _number(default: float, *candidates: Any) -> float:
            chosen = _pick(*candidates)
            if chosen is None:
                return default
            try:
                return float(chosen)
            except (TypeError, ValueError):
                raise ValueError(
                    "Expected a number, got {0!r}".format(chosen))

        def _integer(default: int, *candidates: Any) -> int:
            chosen = _pick(*candidates)
            if chosen is None:
                return default
            try:
                return int(chosen)
            except (TypeError, ValueError):
                raise ValueError(
                    "Expected an integer, got {0!r}".format(chosen))

        backends = (_tuple(data.get("backends"))
                    or _tuple(env.get("FORGE_RUNTIME_BACKENDS"))
                    or ("native",))
        default_backend = str(_pick(data.get("default_backend"),
                                    env.get("FORGE_RUNTIME_BACKEND"))
                              or backends[0])
        # Naming a default backend explicitly also enables it: asking for the
        # Ollama backend by name is an explicit selection, not a surprise.
        if (default_backend in BUILTIN_BACKENDS
                and default_backend not in backends):
            backends = backends + (default_backend,)
        model_dirs = _tuple(data.get("model_dirs"), sep=os.pathsep)
        if not model_dirs:
            model_dirs = _tuple(env.get("FORGE_RUNTIME_MODEL_DIRS"),
                                sep=os.pathsep)

        config = cls(
            default_backend=default_backend,
            backends=backends,
            allow_network=_bool(data.get("allow_network"),
                                _bool(env.get("FORGE_RUNTIME_ALLOW_NETWORK"),
                                      False)),
            ollama_url=str(_pick(data.get("ollama_url"),
                                 env.get("FORGE_RUNTIME_OLLAMA_URL"),
                                 env.get("OLLAMA_BASE_URL"),
                                 env.get("OLLAMA_URL"))
                           or "http://127.0.0.1:11434"),
            ollama_model=str(_pick(data.get("ollama_model"),
                                   env.get("FORGE_RUNTIME_OLLAMA_MODEL"),
                                   env.get("OLLAMA_MODEL")) or ""),
            model_dirs=model_dirs,
            timeout_seconds=_number(DEFAULT_TIMEOUT_SECONDS,
                                    data.get("timeout_seconds"),
                                    env.get("FORGE_RUNTIME_TIMEOUT")),
            max_timeout_seconds=_number(MAX_TIMEOUT_SECONDS,
                                        data.get("max_timeout_seconds"),
                                        env.get("FORGE_RUNTIME_MAX_TIMEOUT")),
            chunk_timeout_seconds=_number(
                DEFAULT_CHUNK_TIMEOUT_SECONDS,
                data.get("chunk_timeout_seconds"),
                env.get("FORGE_RUNTIME_CHUNK_TIMEOUT")),
            health_timeout_seconds=_number(
                DEFAULT_HEALTH_TIMEOUT_SECONDS,
                data.get("health_timeout_seconds"),
                env.get("FORGE_RUNTIME_HEALTH_TIMEOUT")),
            history_size=_integer(200, data.get("history_size"),
                                  env.get("FORGE_RUNTIME_HISTORY_SIZE")),
            retries=_integer(0, data.get("retries"),
                             env.get("FORGE_RUNTIME_RETRIES")),
            retry_backoff_seconds=_number(
                0.25, data.get("retry_backoff_seconds"),
                env.get("FORGE_RUNTIME_RETRY_BACKOFF")),
            max_resident_bytes=_integer(
                0, data.get("max_resident_bytes"),
                env.get("FORGE_RUNTIME_MAX_RESIDENT_BYTES")),
        )
        return config.validate()

    @classmethod
    def load(cls, path: Optional[str] = None, *,
             env: Optional[Mapping[str, str]] = None) -> "RuntimeConfig":
        """Load configuration from a file layered over the environment.

        Looks for ``.forge/runtime.yaml`` then ``.forge/runtime.json`` by
        default.  A *missing* file is not an error: environment defaults
        apply, and the default posture is offline with the native backend.

        A file that exists but cannot be read or parsed **is** an error.
        Silently falling back to defaults would let a one-character typo make
        the runtime ignore the operator's configuration — including which
        backend they asked for — while appearing to work.
        """
        candidates: List[str] = []
        if path:
            candidates.append(path)
        else:
            candidates.extend((".forge/runtime.yaml", ".forge/runtime.json"))

        data: Dict[str, Any] = {}
        for candidate in candidates:
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                raise ValueError(
                    "Cannot read runtime config {0}: {1}".format(
                        candidate, exc))
            if candidate.endswith(".json"):
                try:
                    loaded = json.loads(text)
                except ValueError as exc:
                    raise ValueError(
                        "Runtime config {0} is not valid JSON: {1}".format(
                            candidate, exc))
            else:
                try:
                    import yaml  # type: ignore
                except ImportError:
                    raise ValueError(
                        "Runtime config {0} is YAML but PyYAML is not "
                        "installed; use a .json file or install "
                        "PyYAML.".format(candidate))
                try:
                    loaded = yaml.safe_load(text) or {}
                except Exception as exc:
                    raise ValueError(
                        "Runtime config {0} is not valid YAML: {1}".format(
                            candidate, exc))
            if loaded and not isinstance(loaded, dict):
                raise ValueError(
                    "Runtime config {0} must contain a mapping at the top "
                    "level, got {1}.".format(candidate, type(loaded).__name__))
            if isinstance(loaded, dict):
                data.update(loaded)
        return cls.from_dict(data, env=env)

    def to_dict(self) -> Mapping[str, Any]:
        """A loggable configuration view (contains no secret material)."""
        return {
            "default_backend": self.default_backend,
            "backends": list(self.backends),
            "allow_network": self.allow_network,
            "ollama_url": self.ollama_url,
            "ollama_model": self.ollama_model,
            "model_dirs": list(self.model_dirs),
            "timeout_seconds": self.timeout_seconds,
            "max_timeout_seconds": self.max_timeout_seconds,
            "chunk_timeout_seconds": self.chunk_timeout_seconds,
            "health_timeout_seconds": self.health_timeout_seconds,
            "history_size": self.history_size,
        }


# ---------------------------------------------------------------------------
# Host resource measurement (stdlib only, never raises)
# ---------------------------------------------------------------------------


def _memory_from_status(status: Any) -> Tuple[int, int]:
    """Read total/available bytes from a Windows MEMORYSTATUSEX-shaped object.

    Split out so the parsing is testable without a Windows host.
    """
    try:
        return (int(getattr(status, "ullTotalPhys", 0) or 0),
                int(getattr(status, "ullAvailPhys", 0) or 0))
    except Exception:
        return (0, 0)


def _windows_memory() -> Tuple[int, int]:
    """Total/available physical bytes on Windows, or ``(0, 0)``."""
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return (0, 0)
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return (0, 0)
        return _memory_from_status(status)
    except Exception:
        return (0, 0)


def _posix_memory() -> Tuple[int, int]:
    """Total/available physical bytes via ``os.sysconf``, or ``(0, 0)``."""
    try:
        page = os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
        total_pages = os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
        total = int(page) * int(total_pages)
        available = 0
        try:
            available = int(page) * int(
                os.sysconf("SC_AVPHYS_PAGES"))  # type: ignore[attr-defined]
        except (ValueError, OSError, AttributeError):
            available = _read_meminfo_available()
        return (total, available)
    except (ValueError, OSError, AttributeError):
        return (0, 0)


def _read_meminfo_available() -> int:
    """``MemAvailable`` from ``/proc/meminfo`` when sysconf cannot provide it."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        return 0
    return 0


def system_resources() -> Dict[str, Any]:
    """Measured host resources. Every field degrades to 0/empty, never raises."""
    try:
        cpu_count = int(os.cpu_count() or 0)
    except Exception:
        cpu_count = 0
    total, available = _posix_memory()
    if not total:
        total, available = _windows_memory()
    return {
        "cpu_count": cpu_count,
        "memory_total_bytes": total,
        "memory_available_bytes": available,
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }


# ---------------------------------------------------------------------------
# Model artifact metadata (real headers only; no inference, no fabrication)
# ---------------------------------------------------------------------------

#: Model artifact formats Forge will describe and load.
ALLOWED_MODEL_EXTENSIONS: Tuple[str, ...] = (".gguf", ".safetensors", ".onnx")

#: Refused on sight: these are executables or loader code, not model data.
EXECUTABLE_EXTENSIONS: Tuple[str, ...] = (
    ".exe", ".dll", ".so", ".dylib", ".com", ".bat", ".cmd", ".ps1", ".sh",
    ".py", ".pyc", ".pyd", ".pyw", ".msi", ".jar", ".js", ".vbs", ".vbe",
    ".wsf", ".scr", ".app", ".bin",
)

#: Refused on sight: unpickling executes arbitrary code, so a "model" in a
#: pickle container is an executable-loading vector, not a weight file.
PICKLE_EXTENSIONS: Tuple[str, ...] = (
    ".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle", ".joblib",
)


def classify_artifact(filename: str) -> Tuple[str, str]:
    """Return ``(extension, refusal_reason)`` for a candidate model file.

    ``refusal_reason`` is empty when the file may be described/loaded.
    """
    lowered = filename.lower()
    extension = ""
    for candidate in (".safetensors", ".gguf", ".onnx", ".bin", ".pt", ".pth",
                      ".ckpt", ".pkl", ".pickle", ".joblib", ".exe", ".dll",
                      ".so", ".dylib", ".py", ".pyc", ".pyd", ".sh", ".bat",
                      ".cmd", ".ps1", ".js", ".jar", ".msi"):
        if lowered.endswith(candidate):
            extension = candidate
            break
    if not extension:
        extension = os.path.splitext(lowered)[1]
    if extension in PICKLE_EXTENSIONS:
        return (extension,
                "pickle-based checkpoint formats ({0}) can execute code on "
                "load and are refused; convert to GGUF or "
                "safetensors".format(extension))
    if extension in EXECUTABLE_EXTENSIONS:
        return (extension,
                "{0} is an executable/library format and will not be "
                "loaded".format(extension))
    if extension not in ALLOWED_MODEL_EXTENSIONS:
        return (extension,
                "unsupported model format {0!r}; allowed: {1}".format(
                    extension or "(none)",
                    ", ".join(ALLOWED_MODEL_EXTENSIONS)))
    return (extension, "")


def _read_header(path: str, limit: int) -> bytes:
    """Read at most ``limit`` bytes from the start of ``path``."""
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


class _GgufTruncated(Exception):
    """Raised internally when the scanned window ends mid-value."""


class _GgufReader:
    """Bounded little-endian reader for the GGUF metadata section.

    Values are parsed only far enough to advance the cursor; array contents
    are counted and skipped rather than materialised, so a tokenizer table
    with tens of thousands of entries costs no memory.
    """

    __slots__ = ("data", "pos")

    _SCALARS = {
        0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
        4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 10: ("<Q", 8),
        11: ("<q", 8), 12: ("<d", 8),
    }
    _ARRAY_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                    10: 8, 11: 8, 12: 8}

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.pos = offset

    def _take(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise _GgufTruncated()
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def scalar(self, fmt: str, size: int) -> Any:
        try:
            return struct.unpack(fmt, self._take(size))[0]
        except struct.error:
            raise _GgufTruncated()

    def string(self) -> str:
        length = self.scalar("<Q", 8)
        if length > GGUF_MAX_STRING_BYTES:
            raise _GgufTruncated()
        return self._take(int(length)).decode("utf-8", errors="replace")

    def value(self, vtype: int) -> Any:
        if vtype == 7:  # bool
            return bool(self.scalar("<B", 1))
        if vtype == 8:  # string
            return self.string()
        if vtype == 9:  # array
            elem_type = self.scalar("<I", 4)
            count = self.scalar("<Q", 8)
            if count > GGUF_MAX_ARRAY_ELEMENTS:
                raise _GgufTruncated()
            if elem_type == 8:
                for _ in range(int(count)):
                    self.string()
            elif elem_type == 9:
                for _ in range(int(count)):
                    self.value(9)
            elif elem_type in self._ARRAY_SIZES:
                self._take(int(count) * self._ARRAY_SIZES[elem_type])
            else:
                raise _GgufTruncated()
            return ("array", int(elem_type), int(count))
        spec = self._SCALARS.get(vtype)
        if spec is None:
            raise _GgufTruncated()
        return self.scalar(spec[0], spec[1])


#: GGUF ``general.file_type`` values, named after the GGUF specification.
#: Anything unmapped is reported as ``unknown(<n>)`` rather than guessed.
GGUF_FILE_TYPES: Dict[int, str] = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S",
    15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS",
    20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S",
    25: "IQ4_NL", 26: "IQ3_S", 27: "IQ2_S", 28: "IQ4_XS", 29: "IQ1_M",
    30: "BF16",
}


def describe_gguf(path: str,
                  scan_bytes: int = GGUF_METADATA_SCAN_BYTES
                  ) -> Dict[str, Any]:
    """Read the real GGUF header and metadata key/value section.

    Handles GGUF v1 (32-bit counts) and v2/v3 (64-bit counts) — the layouts
    differ, and misreading one as the other silently yields wrong numbers.
    Everything reported comes from the file; nothing is inferred, and a
    section that cannot be fully parsed is flagged ``metadata_truncated``.
    """
    header = _read_header(path, scan_bytes)
    if len(header) < 8 or header[:4] != b"GGUF":
        return {"format": "gguf", "header_ok": False}
    try:
        version = struct.unpack("<I", header[4:8])[0]
    except struct.error:
        return {"format": "gguf", "header_ok": False}
    if version == 1:
        if len(header) < 16:
            return {"format": "gguf", "header_ok": False}
        try:
            tensor_count, kv_count = struct.unpack("<II", header[8:16])
        except struct.error:
            return {"format": "gguf", "header_ok": False}
        offset = 16
    elif version in (2, 3):
        if len(header) < 24:
            return {"format": "gguf", "header_ok": False}
        try:
            tensor_count, kv_count = struct.unpack("<QQ", header[8:24])
        except struct.error:
            return {"format": "gguf", "header_ok": False}
        offset = 24
    else:
        return {"format": "gguf", "header_ok": False,
                "gguf_version": int(version),
                "detail": "unsupported GGUF version {0}".format(version)}

    result: Dict[str, Any] = {
        "format": "gguf",
        "header_ok": True,
        "gguf_version": int(version),
        "tensor_count": int(tensor_count),
        "metadata_entries": int(kv_count),
    }
    reader = _GgufReader(header, offset)
    parsed = 0
    truncated = False
    general: Dict[str, Any] = {}
    context_window = 0
    embedding_length = 0
    block_count = 0
    try:
        for _ in range(min(int(kv_count), GGUF_MAX_KV_ENTRIES)):
            key = reader.string()
            vtype = reader.scalar("<I", 4)
            value = reader.value(vtype)
            parsed += 1
            if isinstance(value, tuple):
                continue  # array: counted and skipped, not retained
            if key.startswith("general."):
                if len(general) < 32:
                    general[key] = value
            if key.endswith(".context_length") and isinstance(value, int):
                context_window = max(context_window, int(value))
            if key.endswith(".embedding_length") and isinstance(value, int):
                embedding_length = max(embedding_length, int(value))
            if key.endswith(".block_count") and isinstance(value, int):
                block_count = max(block_count, int(value))
    except _GgufTruncated:
        truncated = True
    except Exception:
        truncated = True
    if parsed < int(kv_count):
        # Stopping at the entry cap is truncation too. Reporting a clean
        # parse after deliberately reading only part of the section would
        # let a caller believe it had seen every key in the file.
        truncated = True
    result["metadata_parsed"] = parsed
    # Always present, including when parsing completed cleanly: a missing
    # flag is ambiguous between "fully parsed" and "the parser never ran",
    # and a caller cannot tell those apart without a default of its own.
    result["metadata_truncated"] = bool(truncated)
    if general:
        result["general"] = general
    if context_window:
        result["context_window"] = context_window
    if embedding_length:
        result["embedding_length"] = embedding_length
    if block_count:
        result["block_count"] = block_count
    architecture = general.get("general.architecture")
    if isinstance(architecture, str) and architecture:
        result["architecture"] = architecture[:64]
    name = general.get("general.name")
    if isinstance(name, str) and name:
        result["declared_name"] = name[:128]
    size_label = general.get("general.size_label")
    if isinstance(size_label, str) and size_label:
        result["size_label"] = size_label[:32]
    file_type = general.get("general.file_type")
    if isinstance(file_type, int):
        result["quantization"] = GGUF_FILE_TYPES.get(
            int(file_type), "unknown({0})".format(int(file_type)))
    return result


def describe_safetensors(path: str) -> Dict[str, Any]:
    """Read the real safetensors JSON header (bounded).

    The parameter count is computed from the declared tensor shapes — real
    arithmetic over real header data, not an estimate.
    """
    header = _read_header(path, 8)
    if len(header) < 8:
        return {"format": "safetensors", "header_ok": False}
    try:
        (length,) = struct.unpack("<Q", header)
    except struct.error:
        return {"format": "safetensors", "header_ok": False}
    if length <= 0 or length > MAX_HEADER_BYTES:
        return {"format": "safetensors", "header_ok": False,
                "header_bytes": int(length)}
    body = _read_header(path, 8 + int(length))[8:]
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return {"format": "safetensors", "header_ok": False,
                "header_bytes": int(length)}
    if not isinstance(parsed, dict):
        return {"format": "safetensors", "header_ok": False}
    embedded = parsed.get("__metadata__")
    metadata = dict(embedded) if isinstance(embedded, dict) else {}

    tensors = 0
    parameters = 0
    dtypes: List[str] = []
    for key, value in parsed.items():
        if key == "__metadata__" or not isinstance(value, dict):
            continue
        tensors += 1
        shape = value.get("shape")
        if isinstance(shape, list) and shape:
            product = 1
            for dim in shape:
                if not isinstance(dim, int) or dim < 0:
                    product = 0
                    break
                product *= dim
            parameters += product
        dtype = value.get("dtype")
        if isinstance(dtype, str) and dtype not in dtypes and len(dtypes) < 8:
            dtypes.append(dtype)

    result: Dict[str, Any] = {
        "format": "safetensors",
        "header_ok": True,
        "tensor_count": tensors,
        "header_bytes": int(length),
    }
    if tensors:
        result["parameter_count"] = parameters
        result["dtypes"] = dtypes
    declared_format = metadata.get("format")
    if isinstance(declared_format, str) and declared_format:
        result["declared_format"] = declared_format[:64]
    architecture = (metadata.get("architecture")
                    or metadata.get("model_type") or "")
    if isinstance(architecture, str) and architecture:
        result["declared_architecture"] = architecture[:128]
    return result


def describe_artifact(path: str) -> Dict[str, Any]:
    """Real metadata for one artifact, or an honest ``header_ok: False``."""
    name = os.path.basename(path)
    extension, refusal = classify_artifact(name)
    if refusal:
        return {"format": "unknown", "header_ok": False, "refused": refusal}
    if extension == ".gguf":
        return describe_gguf(path)
    if extension == ".safetensors":
        return describe_safetensors(path)
    return {"format": extension.lstrip("."), "header_ok": False,
            "detail": "format is not introspected by the native runtime"}


# ---------------------------------------------------------------------------
# Backend contract
# ---------------------------------------------------------------------------


class ModelBackend:
    """The backend contract: how one execution technology is driven.

    Subclass this (or duck-type it) to add an execution technology.  A
    backend is *infrastructure*: it must never synthesise model output that
    the underlying model did not produce.  Partial implementations inherit
    honest defaults — an unimplemented capability reports itself
    unavailable rather than faking a result.
    """

    #: Unique name inside one runtime. Must be non-empty.
    name: str = ""
    kind: str = BackendKind.CUSTOM.value
    description: str = ""
    local: bool = True
    #: ``True`` when any of this backend's operations touch the network.
    requires_network: bool = False

    def validate(self) -> None:
        """Reject a backend that cannot honour the contract."""
        if not self.name or not str(self.name).strip():
            raise BackendProtocolError("A backend name is required.")
        if not str(self.name).strip() == self.name:
            raise BackendProtocolError(
                "Backend name {0!r} must not have surrounding "
                "whitespace.".format(self.name))
        if ":" in self.name:
            raise BackendProtocolError(
                "Backend name {0!r} must not contain ':' (it separates "
                "backend from model in model ids).".format(self.name))
        for required in ("available", "list_models", "load_model",
                         "unload_model", "generate", "stream", "health"):
            if not callable(getattr(self, required, None)):
                raise BackendProtocolError(
                    "Backend {0!r} does not implement {1}().".format(
                        self.name, required))

    # -- availability ----------------------------------------------------

    def available(self) -> Tuple[bool, str]:
        """``(is_available, human_readable_detail)``."""
        return (False, "{0} is not available.".format(self.name or "backend"))

    # -- discovery / metadata -------------------------------------------

    def list_models(self) -> List[RuntimeModel]:
        return []

    def load_model(self, model: RuntimeModel,
                   token: Optional[CancellationToken] = None
                   ) -> RuntimeModel:
        raise BackendUnavailableError(
            "{0} does not support model loading.".format(self.name))

    def unload_model(self, model_id: str) -> bool:
        return False

    # -- inference -------------------------------------------------------

    def generate(self, request: RuntimeRequest,
                 token: Optional[CancellationToken] = None
                 ) -> RuntimeResponse:
        raise BackendUnavailableError(
            "{0} cannot run inference.".format(self.name))

    def stream(self, request: RuntimeRequest,
               token: Optional[CancellationToken] = None
               ) -> Iterator[Any]:
        raise BackendUnavailableError(
            "{0} cannot stream inference.".format(self.name))
        yield  # pragma: no cover - makes this a generator

    # -- ops -------------------------------------------------------------

    def health(self, probe: bool = True) -> RuntimeHealth:
        available, detail = self.available()
        return RuntimeHealth(
            backend=self.name, kind=self.kind,
            status=(RuntimeState.READY.value if available
                    else RuntimeState.UNAVAILABLE.value),
            checked_at=time.time(), detail=detail)

    def resources(self) -> Dict[str, Any]:
        return {}

    def info(self) -> RuntimeBackendInfo:
        available, detail = self.available()
        return RuntimeBackendInfo(
            name=self.name, kind=self.kind, local=self.local,
            requires_network=self.requires_network, available=available,
            detail=detail, description=self.description)


class InferenceAdapter:
    """Contract for a real inference engine behind :class:`NativeBackend`.

    The runtime never imports an engine by itself: an adapter instance must
    be handed to the backend by the host process.  That is what keeps
    "no arbitrary executable loading" true even for first-party inference.
    """

    name: str = ""

    def available(self) -> Tuple[bool, str]:
        return (False, "No inference adapter is configured.")

    def generate(self, request: RuntimeRequest,
                 token: Optional[CancellationToken] = None
                 ) -> RuntimeResponse:
        raise BackendUnavailableError(
            "This inference adapter cannot run inference.")

    def stream(self, request: RuntimeRequest,
               token: Optional[CancellationToken] = None) -> Iterator[Any]:
        raise BackendUnavailableError(
            "This inference adapter cannot stream inference.")
        yield  # pragma: no cover - makes this a generator


_NATIVE_NO_ADAPTER = (
    "No native inference adapter is configured. The native backend can "
    "discover and describe model artifacts, but it does not itself run "
    "inference and will not fabricate output. Provide an "
    "InferenceAdapter instance, enable the Ollama backend, or register a "
    "custom backend.")


class NativeBackend(ModelBackend):
    """First-party runtime backend.

    What it genuinely does:

    * **Discovery** of model artifacts inside explicitly allowed
      directories, bounded by depth and extension allowlist.
    * **Metadata** read from the real file header (GGUF magic/version and
      tensor counts; safetensors JSON header).
    * **Load/unload abstraction**: admitting an artifact into the resident
      set and releasing it, so callers have one lifecycle API regardless of
      the engine underneath.

    What it deliberately does *not* do: run tensors.  Generation is
    delegated to an :class:`InferenceAdapter` supplied by the host process.
    With no adapter, generation fails with an honest
    :class:`BackendUnavailableError` instead of returning invented text —
    the runtime is not a model.
    """

    name = "native"
    kind = BackendKind.NATIVE.value
    description = ("First-party Forge model runtime: artifact discovery and "
                   "metadata, with inference delegated to an explicitly "
                   "provided adapter.")
    local = True
    requires_network = False

    def __init__(self, model_dirs: Sequence[str] = (),
                 extensions: Sequence[str] = ALLOWED_MODEL_EXTENSIONS,
                 max_depth: int = 4, max_files: int = 1024,
                 adapter: Optional[InferenceAdapter] = None,
                 max_resident_bytes: int = 0) -> None:
        self.model_dirs: Tuple[str, ...] = tuple(model_dirs or ())
        self.extensions: Tuple[str, ...] = tuple(
            ext.lower() for ext in (extensions or ALLOWED_MODEL_EXTENSIONS))
        self.max_depth = max(1, int(max_depth))
        self.max_files = max(1, int(max_files))
        self.adapter = adapter
        self.max_resident_bytes = max(0, int(max_resident_bytes or 0))
        self._loaded: Dict[str, RuntimeModel] = {}
        self._invalid_dirs: Tuple[str, ...] = ()
        self._lock = threading.RLock()

    # -- filesystem containment ------------------------------------------

    def _resolved_dirs(self) -> List[str]:
        """Resolve configured model directories.

        Directories that cannot be resolved or do not exist are recorded in
        :attr:`invalid_dirs` rather than silently dropped: a typo in
        ``model_dirs`` would otherwise present as "no models installed",
        which sends the operator looking in the wrong place.
        """
        resolved: List[str] = []
        invalid: List[str] = []
        for entry in self.model_dirs:
            try:
                candidate = os.path.realpath(os.path.abspath(
                    os.path.expanduser(str(entry))))
            except (OSError, ValueError):
                invalid.append(str(entry))
                continue
            if os.path.isdir(candidate):
                resolved.append(candidate)
            else:
                invalid.append(str(entry))
        with self._lock:
            self._invalid_dirs = tuple(dict.fromkeys(invalid))
        return resolved

    def invalid_dirs(self) -> Tuple[str, ...]:
        """Configured model directories that are missing or unreadable.

        Refreshes on every call rather than returning a stale cache: the
        answer is only meaningful as of now, and returning an empty tuple
        before anything had scanned would read as "all directories are
        fine".
        """
        self._resolved_dirs()
        with self._lock:
            return self._invalid_dirs

    def assert_inside(self, path: str) -> str:
        """Return the resolved path, or refuse to touch anything outside.

        Enforces "no unrestricted filesystem access": the file must live
        inside an explicitly allowed model directory and must not be a
        symlink pointing out of it.
        """
        allowed = self._resolved_dirs()
        if not allowed:
            raise RuntimeSecurityError(
                "No model directories are configured, so no model path may "
                "be accessed. Set runtime.model_dirs explicitly.")
        try:
            target = os.path.abspath(os.path.expanduser(str(path)))
            real = os.path.realpath(target)
        except (OSError, ValueError) as exc:
            raise RuntimeSecurityError(
                "Model path could not be resolved: {0}".format(exc)) from exc
        if ".." in str(path).replace("\\", "/").split("/"):
            raise RuntimeSecurityError(
                "Model paths must not contain '..': {0}".format(path))
        for root in allowed:
            if real == root or real.startswith(root + os.sep):
                return real
        raise RuntimeSecurityError(
            "Refusing to access {0}: it is outside the configured model "
            "directories.".format(path))

    def assert_loadable(self, path: str) -> str:
        """Refuse executables and pickle checkpoints; allow safe formats."""
        real = self.assert_inside(path)
        extension, refusal = classify_artifact(os.path.basename(real))
        if refusal:
            raise RuntimeSecurityError(refusal)
        if extension not in self.extensions:
            raise RuntimeSecurityError(
                "Model format {0!r} is not enabled for this backend "
                "(allowed: {1}).".format(extension, ", ".join(self.extensions)))
        return real

    # -- discovery --------------------------------------------------------

    def _scan(self, root: str) -> Iterator[str]:
        """Breadth-bounded directory scan (never follows symlinked dirs)."""
        seen = 0
        stack: List[Tuple[str, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = sorted(os.listdir(current))
            except OSError:
                continue
            for entry in entries:
                full = os.path.join(current, entry)
                try:
                    if os.path.islink(full):
                        continue  # never follow links out of the allowlist
                    if os.path.isdir(full):
                        if depth + 1 <= self.max_depth:
                            stack.append((full, depth + 1))
                        continue
                    if not os.path.isfile(full):
                        continue
                except OSError:
                    continue
                lowered = entry.lower()
                if not any(lowered.endswith(ext) for ext in self.extensions):
                    continue
                seen += 1
                if seen > self.max_files:
                    return
                yield full

    def list_models(self) -> List[RuntimeModel]:
        models: Dict[str, RuntimeModel] = {}
        with self._lock:
            loaded = dict(self._loaded)
        found: List[Tuple[str, str]] = []
        for root in self._resolved_dirs():
            for path in self._scan(root):
                found.append((root, path))
        for path, name in self._assign_names(found):
            model = self._describe(path, name)
            existing = loaded.get(model.model_id)
            if existing is not None:
                model.loaded = True
            models[model.model_id] = model
        return [models[key] for key in sorted(models)]

    @staticmethod
    def _real(path: str) -> str:
        try:
            return os.path.realpath(path)
        except OSError:
            return path

    @staticmethod
    def _relative_name(root: str, path: str) -> str:
        """Path relative to its search root, with ``/`` separators."""
        try:
            rel = os.path.relpath(path, root)
        except ValueError:  # different drives on Windows
            return os.path.basename(path)
        if rel in (os.curdir, "") or rel.startswith(os.pardir):
            return os.path.basename(path)
        return rel.replace(os.sep, "/")

    def _assign_names(self, found: List[Tuple[str, str]]
                      ) -> List[Tuple[str, str]]:
        """Give every discovered artifact a stable, collision-free name.

        The bare basename alone would let ``a/same.gguf`` and ``b/same.gguf``
        collapse into a single entry, silently discarding a real model from
        discovery.  Names are only widened when a genuine collision exists,
        so a normal single-copy install keeps plain filenames:

        1. unique basename -> the basename;
        2. otherwise the path relative to its search root;
        3. if that still collides (the same relative path under two roots),
           qualify it with the root directory's name;
        4. a final ``#N`` suffix covers the pathological remainder, so no
           artifact can ever be dropped.

        Deterministic: candidates are processed in sorted path order.
        """
        by_base: Dict[str, Set[str]] = {}
        for _root, path in found:
            by_base.setdefault(os.path.basename(path), set()).add(
                self._real(path))

        used: Dict[str, str] = {}
        assigned: List[Tuple[str, str]] = []
        for root, path in sorted(found, key=lambda item: item[1]):
            real = self._real(path)
            base = os.path.basename(path)
            if len(by_base.get(base, ())) <= 1:
                name = base
            else:
                name = self._relative_name(root, path)
                taken = used.get(name)
                if taken is not None and taken != real:
                    qualifier = os.path.basename(os.path.normpath(root))
                    name = "{0}/{1}".format(qualifier or root, name)
            counter = 1
            unique = name
            while unique in used and used[unique] != real:
                counter += 1
                unique = "{0}#{1}".format(name, counter)
            used[unique] = real
            assigned.append((path, unique))
        return assigned

    def _describe(self, path: str, name: str) -> RuntimeModel:
        try:
            size = int(os.path.getsize(path))
        except OSError:
            size = 0
        described = describe_artifact(path) if size <= MAX_ARTIFACT_BYTES else {
            "format": "unknown", "header_ok": False,
            "detail": "artifact exceeds the runtime size bound"}
        stem = name
        for ext in self.extensions:
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        metadata: Dict[str, Any] = {
            "header_ok": bool(described.get("header_ok")),
            "source": "native-discovery",
        }
        for key in ("gguf_version", "tensor_count", "metadata_entries",
                    "metadata_parsed", "metadata_truncated", "header_bytes",
                    "declared_format", "declared_architecture",
                    "declared_name", "size_label", "quantization",
                    "embedding_length", "block_count", "parameter_count",
                    "dtypes", "detail", "refused"):
            if key in described:
                metadata[key] = described[key]
        return RuntimeModel(
            model_id=RuntimeModel.make_id(self.name, name),
            name=name,
            backend=self.name,
            kind=str(described.get("architecture") or "unknown"),
            context_window=int(described.get("context_window") or 0),
            max_output_tokens=0,
            size_bytes=size,
            format=str(described.get("format", "unknown")),
            local=True,
            loaded=False,
            path=path,
            discovered_at=time.time(),
            metadata=metadata,
        )

    # -- load / unload ----------------------------------------------------

    def resident_bytes(self) -> int:
        """Total declared size of the artifacts currently resident."""
        with self._lock:
            return sum(int(m.size_bytes or 0) for m in self._loaded.values())

    def load_model(self, model: RuntimeModel,
                   token: Optional[CancellationToken] = None
                   ) -> RuntimeModel:
        """Admit an artifact into the resident set (metadata-verified)."""
        if token is not None:
            token.raise_if_cancelled()
        if not (model.path or "").strip():
            # Falling back to ``model.name`` here would produce a bogus path
            # and a confusing "outside the configured model directories"
            # error.  The real problem is that this model carries no
            # artifact location, and only re-discovery can supply one.
            raise ModelNotFoundError(
                "Model {0!r} has no artifact path; run discovery to resolve "
                "its location before loading it.".format(model.model_id))
        path = self.assert_loadable(model.path)
        with self._lock:
            resident = self._describe(path, model.name
                                      or os.path.basename(path))
            if self.max_resident_bytes > 0:
                projected = sum(int(m.size_bytes or 0)
                                for key, m in self._loaded.items()
                                if key != resident.model_id)
                projected += int(resident.size_bytes or 0)
                if projected > self.max_resident_bytes:
                    raise RuntimeCapacityError(
                        "Loading {0} would use {1} bytes of resident memory, "
                        "exceeding the configured cap of {2} bytes. Unload "
                        "another model or raise "
                        "max_resident_bytes.".format(
                            resident.model_id, projected,
                            self.max_resident_bytes))
            resident.loaded = True
            self._loaded[resident.model_id] = resident
            return resident

    def unload_model(self, model_id: str) -> bool:
        with self._lock:
            return self._loaded.pop(model_id, None) is not None

    def loaded_models(self) -> List[RuntimeModel]:
        with self._lock:
            return list(self._loaded.values())

    # -- inference --------------------------------------------------------

    def _adapter_error(self) -> BackendUnavailableError:
        if self.adapter is None:
            return BackendUnavailableError(_NATIVE_NO_ADAPTER)
        return BackendUnavailableError(
            "Native inference adapter {0!r} is unavailable.".format(
                getattr(self.adapter, "name", "") or "adapter"))

    def generate(self, request: RuntimeRequest,
                 token: Optional[CancellationToken] = None
                 ) -> RuntimeResponse:
        if self.adapter is None:
            raise self._adapter_error()
        if token is not None:
            token.raise_if_cancelled()
        return self.adapter.generate(request, token)

    def stream(self, request: RuntimeRequest,
               token: Optional[CancellationToken] = None) -> Iterator[Any]:
        if self.adapter is None:
            raise self._adapter_error()
        streamer = getattr(self.adapter, "stream", None)
        if not callable(streamer):
            raise BackendUnavailableError(
                "Native inference adapter {0!r} cannot stream.".format(
                    getattr(self.adapter, "name", "") or "adapter"))
        if token is not None:
            token.raise_if_cancelled()
        for chunk in streamer(request, token):
            # Forward the chunk *before* consulting the token. It is real
            # output the adapter already produced; dropping it here would
            # make a cancelled stream report text it never had, and the
            # stream layer above is what decides what to do with it. The
            # token is still checked every iteration, so stopping stays
            # prompt.
            yield chunk
            if token is not None and token.cancelled:
                return

    # -- ops --------------------------------------------------------------

    def available(self) -> Tuple[bool, str]:
        """Discovery is always available; generation needs an adapter."""
        detail_parts: List[str] = []
        if self.adapter is None:
            detail_parts.append("Artifact discovery only: "
                                + _NATIVE_NO_ADAPTER)
        else:
            try:
                ok, detail = self.adapter.available()
            except Exception as exc:  # broken adapter must not break runtime
                return (False, "Inference adapter failed: {0}".format(
                    _error_text(exc)))
            if not ok:
                detail_parts.append(str(detail))
        invalid = self.invalid_dirs()
        if invalid:
            detail_parts.append(
                "Missing or unreadable model_dirs: {0}".format(
                    ", ".join(invalid)))
        return (True, " ".join(detail_parts) if detail_parts
                else "Adapter {0} is available.".format(
                    getattr(self.adapter, "name", "") or "inference"))

    def health(self, probe: bool = True) -> RuntimeHealth:
        """Discovery-only is *degraded*, never *ready*: it cannot infer."""
        if self.adapter is None:
            status = RuntimeState.DEGRADED.value
            detail = _NATIVE_NO_ADAPTER
        else:
            try:
                adapter_ready, detail = self.adapter.available()
            except Exception as exc:
                adapter_ready = False
                detail = "Inference adapter failed: {0}".format(
                    _error_text(exc))
            status = (RuntimeState.READY.value if adapter_ready
                      else RuntimeState.UNAVAILABLE.value)
        models = self.list_models() if probe else []
        invalid = self.invalid_dirs()
        if invalid:
            # A typo in model_dirs otherwise reports as "no models", sending
            # the operator hunting for a download problem instead.
            detail = "{0} Missing or unreadable model_dirs: {1}".format(
                detail, ", ".join(invalid)).strip()
            if status == RuntimeState.READY.value:
                status = RuntimeState.DEGRADED.value
        return RuntimeHealth(
            backend=self.name, kind=self.kind, status=status,
            checked_at=time.time(), detail=detail,
            models_available=len(models),
            models_loaded=len(self.loaded_models()))

    def resources(self) -> Dict[str, Any]:
        loaded = self.loaded_models()
        return {
            "loaded_models": len(loaded),
            "resident_bytes": sum(int(m.size_bytes or 0) for m in loaded),
            "model_dirs": list(self.model_dirs),
            "invalid_model_dirs": list(self.invalid_dirs()),
            "max_resident_bytes": self.max_resident_bytes,
            "inference_adapter": (getattr(self.adapter, "name", "")
                                  if self.adapter is not None else ""),
        }


class OllamaBackend(ModelBackend):
    """Ollama HTTP backend.

    Talks to the Ollama API with the standard library only.  Network access
    is refused until :attr:`allow_network` is explicitly ``True``, so a
    runtime built with default configuration never reaches the network —
    discovery, health, and generation all report the backend as
    unavailable instead.
    """

    name = "ollama"
    kind = BackendKind.OLLAMA.value
    description = ("Ollama local server (/api/tags, /api/generate). "
                   "Requires explicitly enabled network access.")
    local = True
    requires_network = True

    #: Endpoint paths used. Nothing else is ever requested.
    TAGS_PATH = "/api/tags"
    GENERATE_PATH = "/api/generate"

    def __init__(self, base_url: str = "http://127.0.0.1:11434",
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 allow_network: bool = False, default_model: str = "",
                 health_timeout: float = DEFAULT_HEALTH_TIMEOUT_SECONDS
                 ) -> None:
        self.timeout = max(0.5, float(timeout))
        self.health_timeout = max(0.5, float(health_timeout))
        self.allow_network = bool(allow_network)
        self.default_model = str(default_model or "")
        self.base_url = self._normalize(base_url)

    @staticmethod
    def _normalize(base_url: str) -> str:
        """Validate and normalise an endpoint. Refuses credentials in a URL."""
        url = str(base_url or "").strip()
        if not url:
            raise ValueError("An Ollama base URL is required.")
        lowered = url.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise RuntimeSecurityError(
                "Refusing Ollama URL {0!r}: only http/https endpoints are "
                "allowed.".format(url))
        if "@" in url:
            raise RuntimeSecurityError(
                "Refusing Ollama URL with embedded credentials; configure "
                "authentication out of band.")
        while url.endswith("/"):
            url = url[:-1]
        for path in (OllamaBackend.GENERATE_PATH, OllamaBackend.TAGS_PATH):
            if url.endswith(path):
                url = url[: -len(path)]
                break
        return url

    # -- network plumbing -------------------------------------------------

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _require_network(self) -> None:
        if not self.allow_network:
            raise BackendUnavailableError(
                "Network access is not enabled for the Ollama backend. Set "
                "runtime.allow_network (or FORGE_RUNTIME_ALLOW_NETWORK=1) to "
                "permit the explicit endpoint {0}.".format(self.base_url))

    def _request(self, path: str, payload: Optional[bytes] = None,
                 timeout: Optional[float] = None) -> Any:
        self._require_network()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json"}
        request = urllib.request.Request(self._url(path), payload, headers)
        limit = self.timeout if timeout is None else timeout
        with urllib.request.urlopen(request, timeout=limit) as response:
            return response.read().decode("utf-8", errors="replace")

    def _stream_request(self, path: str, payload: bytes, timeout: float):
        self._require_network()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/x-ndjson"}
        request = urllib.request.Request(self._url(path), payload, headers)
        return urllib.request.urlopen(request, timeout=timeout)

    # -- request body -----------------------------------------------------

    def _body(self, request: RuntimeRequest, stream: bool) -> Dict[str, Any]:
        model = self._model_name(request)
        body: Dict[str, Any] = {"model": model, "stream": bool(stream)}
        system = request.system.strip() if request.system else ""
        if system:
            body["system"] = system
        elif request.task and request.task.strip():
            body["system"] = request.task.strip()
        body["prompt"] = request.compose_prompt()
        options: Dict[str, Any] = {}
        if request.max_output_tokens is not None:
            options["num_predict"] = int(request.max_output_tokens)
        if request.temperature is not None:
            options["temperature"] = float(request.temperature)
        if request.seed is not None:
            options["seed"] = int(request.seed)
        if request.stop:
            options["stop"] = list(request.stop)
        if options:
            body["options"] = options
        return body

    def _model_name(self, request: RuntimeRequest) -> str:
        model = (request.model or "").strip()
        if ":" in model and model.split(":", 1)[0] == self.name:
            model = model.split(":", 1)[1]
        return model or self.default_model

    # -- availability -----------------------------------------------------

    def available(self) -> Tuple[bool, str]:
        if not self.allow_network:
            return (False,
                    "Network access is disabled; endpoint {0} will not be "
                    "contacted.".format(self.base_url))
        return (True, "Endpoint {0} (not probed).".format(self.base_url))

    # -- discovery --------------------------------------------------------

    def list_models(self) -> List[RuntimeModel]:
        raw = self._request(self.TAGS_PATH, timeout=self.health_timeout)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise BackendUnavailableError(
                "Ollama returned an unparsable model list: {0}".format(
                    _error_text(exc))) from exc
        models: List[RuntimeModel] = []
        for item in data.get("models", []) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("model") or "").strip()
            if not name:
                continue
            details = item.get("details")
            details = details if isinstance(details, dict) else {}
            models.append(RuntimeModel(
                model_id=RuntimeModel.make_id(self.name, name),
                name=name,
                backend=self.name,
                kind="unknown",
                size_bytes=int(item.get("size") or 0),
                format="gguf",
                quantization=str(details.get("quantization_level") or ""),
                parameters=str(details.get("parameter_size") or ""),
                local=True,
                discovered_at=time.time(),
                metadata={
                    "family": str(details.get("family") or ""),
                    "context_window": int(
                        details.get("context_window") or 0) or 0,
                    "source": "ollama-tags",
                },
            ))
        models.sort(key=lambda model: model.name)
        return models

    def load_model(self, model: RuntimeModel,
                   token: Optional[CancellationToken] = None
                   ) -> RuntimeModel:
        # Ollama pulls a model into memory on first use. Forge never pulls
        # (that would be an unbounded, silent download); loading is a no-op
        # that records intent, and generation is what actually loads it.
        loaded = RuntimeModel(
            model_id=model.model_id or RuntimeModel.make_id(self.name,
                                                            model.name),
            name=model.name, backend=self.name, kind=model.kind,
            size_bytes=model.size_bytes, format=model.format or "gguf",
            quantization=model.quantization, parameters=model.parameters,
            local=True, loaded=True, discovered_at=time.time(),
            metadata=dict(model.metadata or {}),
        )
        loaded.metadata["load_note"] = (
            "Ollama loads weights on first request; the runtime does not "
            "pull models.")
        return loaded

    def unload_model(self, model_id: str) -> bool:
        return True  # Ollama evicts on its own keep-alive; nothing to release

    # -- inference --------------------------------------------------------

    def generate(self, request: RuntimeRequest,
                 token: Optional[CancellationToken] = None
                 ) -> RuntimeResponse:
        started = time.perf_counter()
        if token is not None:
            token.raise_if_cancelled()
        model = self._model_name(request)
        if not model:
            raise BackendUnavailableError(
                "No Ollama model was requested and no default is configured.")
        payload = json.dumps(self._body(request, stream=False)).encode("utf-8")
        raw = self._request(self.GENERATE_PATH, payload,
                            timeout=request.timeout or self.timeout)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise BackendUnavailableError(
                "Ollama returned an unparsable completion: {0}".format(
                    _error_text(exc))) from exc
        text = str(data.get("response", "") or "")
        finish = "stop"
        if data.get("done_reason"):
            finish = str(data["done_reason"])
        elif data.get("done") is False:
            finish = FinishReason.LENGTH.value
        return RuntimeResponse(
            text=text, model=model, backend=self.name, success=True,
            request_id=request.request_id, trace_id=request.trace_id,
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            finish_reason=finish,
            raw=None,
            metadata={"total_duration_ns": int(data.get("total_duration") or 0)},
        )

    def stream(self, request: RuntimeRequest,
               token: Optional[CancellationToken] = None) -> Iterator[Any]:
        if token is not None:
            token.raise_if_cancelled()
        model = self._model_name(request)
        if not model:
            raise BackendUnavailableError(
                "No Ollama model was requested and no default is configured.")
        payload = json.dumps(self._body(request, stream=True)).encode("utf-8")
        response = self._stream_request(
            self.GENERATE_PATH, payload,
            timeout=max(self.health_timeout, request.timeout or self.timeout))
        try:
            for raw_line in response:
                if token is not None:
                    token.raise_if_cancelled()
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                chunk = str(data.get("response", "") or "")
                if chunk:
                    yield RuntimeChunk(
                        text=chunk, request_id=request.request_id,
                        input_tokens=int(data.get("prompt_eval_count") or 0),
                        output_tokens=int(data.get("eval_count") or 0))
                if data.get("done"):
                    return
        except OSError as exc:
            # Covers socket.error, urllib.error.URLError and HTTPError.
            # ``generate()`` already normalises transport failures to
            # ``BackendUnavailableError``; a reset mid-stream must not be the
            # one place a raw socket error escapes to the caller.
            raise BackendUnavailableError(
                "Ollama stream failed for endpoint {0}: {1}".format(
                    self.base_url, _error_text(exc))) from exc
        finally:
            try:
                response.close()
            except Exception:
                pass

    # -- ops --------------------------------------------------------------

    def health(self, probe: bool = True) -> RuntimeHealth:
        health = RuntimeHealth(
            backend=self.name, kind=self.kind,
            status=RuntimeState.UNKNOWN.value, checked_at=time.time(),
            requires_network=True, network_allowed=self.allow_network,
            detail="Endpoint {0}.".format(self.base_url))
        if not self.allow_network:
            health.status = RuntimeState.UNAVAILABLE.value
            health.detail = ("Network access is disabled; the runtime did not "
                             "contact {0}.".format(self.base_url))
            return health
        if not probe:
            health.status = RuntimeState.READY.value
            health.detail = "Not probed (network enabled)."
            return health
        started = time.perf_counter()
        try:
            models = self.list_models()
        except Exception as exc:
            health.status = RuntimeState.UNAVAILABLE.value
            health.error = _error_text(exc)
            health.detail = "Endpoint {0} is not reachable.".format(
                self.base_url)
            health.latency_ms = (time.perf_counter() - started) * 1000.0
            return health
        health.status = RuntimeState.READY.value
        health.latency_ms = (time.perf_counter() - started) * 1000.0
        health.models_available = len(models)
        health.detail = "{0} model(s) served by {1}.".format(
            len(models), self.base_url)
        return health

    def resources(self) -> Dict[str, Any]:
        return {"endpoint": self.base_url,
                "network_allowed": self.allow_network,
                "timeout_seconds": self.timeout}


class ExternalClientBackend(ModelBackend):
    """Adapts an explicitly injected client into the backend contract.

    This is the seam for future backends — llama.cpp, a first-party Forge
    inference server, or an in-house engine.  The runtime never imports
    such a library itself: the host process constructs the client and hands
    it over, which keeps "no arbitrary executable loading" true.  Without a
    client the backend reports itself unavailable and generation fails
    honestly.

    The client is duck-typed and only these members are used:

    * ``available() -> (bool, str)`` (optional)
    * ``list_models() -> [RuntimeModel]`` (optional)
    * ``load_model(model, token)`` / ``unload_model(model_id)`` (optional)
    * ``generate(request, token) -> RuntimeResponse`` (required to infer)
    * ``stream(request, token) -> iterator`` (optional)
    """

    def __init__(self, name: str, client: Any = None,
                 kind: str = BackendKind.CUSTOM.value,
                 description: str = "", local: bool = True,
                 requires_network: bool = False) -> None:
        self.name = name
        self.kind = kind
        self.description = description or (
            "{0} backend driven by an explicitly provided client.".format(name))
        self.local = local
        self.requires_network = requires_network
        self.client = client

    # -- helpers ----------------------------------------------------------

    def _call(self, member: str, *args: Any) -> Any:
        target = getattr(self.client, member, None)
        if not callable(target):
            raise BackendUnavailableError(
                "{0} client does not implement {1}().".format(self.name,
                                                              member))
        return target(*args)

    def available(self) -> Tuple[bool, str]:
        if self.client is None:
            return (False,
                    "{0} needs an explicitly provided client; the runtime "
                    "does not load third-party libraries by itself.".format(
                        self.name))
        probe = getattr(self.client, "available", None)
        if callable(probe):
            try:
                ok, detail = probe()
            except Exception as exc:
                return (False, "Client probe failed: {0}".format(
                    _error_text(exc)))
            return (bool(ok), str(detail))
        return (True, "Client registered for {0}.".format(self.name))

    def list_models(self) -> List[RuntimeModel]:
        if self.client is None:
            return []
        found = self._call("list_models")
        models: List[RuntimeModel] = []
        for item in found or []:
            if isinstance(item, RuntimeModel):
                model = item
            elif isinstance(item, str):
                model = RuntimeModel(
                    model_id=RuntimeModel.make_id(self.name, item),
                    name=item, backend=self.name)
            elif isinstance(item, dict):
                model = RuntimeModel(
                    model_id=str(item.get("model_id")
                                 or RuntimeModel.make_id(
                                     self.name, str(item.get("name", "")))),
                    name=str(item.get("name", "")), backend=self.name,
                    kind=str(item.get("kind", "unknown")),
                    context_window=int(item.get("context_window") or 0),
                    size_bytes=int(item.get("size_bytes") or 0),
                    format=str(item.get("format", "unknown")),
                    metadata=dict(item.get("metadata") or {}))
            else:
                continue
            if not model.backend:
                model.backend = self.name
            if not model.model_id:
                model.model_id = RuntimeModel.make_id(self.name, model.name)
            models.append(model)
        return models

    def load_model(self, model: RuntimeModel,
                   token: Optional[CancellationToken] = None
                   ) -> RuntimeModel:
        result = self._call("load_model", model, token)
        return result if isinstance(result, RuntimeModel) else model

    def unload_model(self, model_id: str) -> bool:
        return bool(self._call("unload_model", model_id))

    def generate(self, request: RuntimeRequest,
                 token: Optional[CancellationToken] = None
                 ) -> RuntimeResponse:
        if self.client is None:
            raise BackendUnavailableError(
                "{0} has no client; it cannot run inference.".format(self.name))
        if token is not None:
            token.raise_if_cancelled()
        result = self._call("generate", request, token)
        if not isinstance(result, RuntimeResponse):
            raise BackendProtocolError(
                "{0}.generate() must return a RuntimeResponse.".format(
                    self.name))
        return result

    def stream(self, request: RuntimeRequest,
               token: Optional[CancellationToken] = None) -> Iterator[Any]:
        if self.client is None:
            raise BackendUnavailableError(
                "{0} has no client; it cannot stream inference.".format(
                    self.name))
        if token is not None:
            token.raise_if_cancelled()
        for chunk in self._call("stream", request, token):
            if token is not None and token.cancelled:
                return
            yield chunk


class LlamaCppBackend(ExternalClientBackend):
    """llama.cpp backend (future).

    Deliberately inert until a host supplies a client.  ``llama_cpp`` is
    never imported here: it is an optional dependency and importing a
    native extension on the user's behalf would violate the runtime's
    executable-loading rule.
    """

    def __init__(self, client: Any = None, **kwargs: Any) -> None:
        kwargs.setdefault(
            "description",
            "llama.cpp backend. Requires an explicitly provided client; the "
            "optional binding is never imported automatically.")
        super().__init__("llama_cpp", client=client,
                         kind=BackendKind.LLAMA_CPP.value, **kwargs)


class ForgeInferenceBackend(ExternalClientBackend):
    """Custom Forge backend (future first-party inference server).

    Same rule as llama.cpp: inert until a client is explicitly provided.
    """

    def __init__(self, client: Any = None, **kwargs: Any) -> None:
        kwargs.setdefault(
            "description",
            "Custom Forge inference backend. Requires an explicitly provided "
            "client; nothing is imported or executed automatically.")
        kwargs.setdefault("requires_network", True)
        super().__init__("forge", client=client,
                         kind=BackendKind.FORGE.value, **kwargs)


#: Name -> factory for built-in backends. This is the *only* way a backend is
#: created from a string; there is no import-by-name path.
_BACKEND_FACTORIES: Dict[str, Callable[..., ModelBackend]] = {
    "native": lambda config: NativeBackend(
        model_dirs=config.model_dirs,
        max_resident_bytes=config.max_resident_bytes),
    "ollama": lambda config: OllamaBackend(
        base_url=config.ollama_url, timeout=config.timeout_seconds,
        allow_network=config.allow_network,
        default_model=config.ollama_model,
        health_timeout=config.health_timeout_seconds),
    "llama_cpp": lambda config: LlamaCppBackend(),
    "forge": lambda config: ForgeInferenceBackend(),
}


def create_backend(name: str, config: Optional[RuntimeConfig] = None,
                   client: Any = None) -> ModelBackend:
    """Build one built-in backend by name.

    ``name`` must be in :data:`BUILTIN_BACKENDS`.  Anything else raises
    :class:`BackendNotFoundError` listing the allowlist — an arbitrary
    dotted path is never imported.
    """
    key = str(name or "").strip()
    if key not in _BACKEND_FACTORIES:
        raise BackendNotFoundError(
            "Unknown backend {0!r}. Built-in backends: {1}. A custom backend "
            "must be constructed in code and passed to "
            "ModelRuntime.register_backend().".format(
                name, ", ".join(BUILTIN_BACKENDS)))
    configuration = config or RuntimeConfig()
    if key == "llama_cpp":
        return LlamaCppBackend(client=client)
    if key == "forge":
        return ForgeInferenceBackend(client=client)
    return _BACKEND_FACTORIES[key](configuration)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

_DONE = object()
#: Sentinel for "the sliced queue wait expired without an item", kept
#: distinct from every real chunk and from ``_DONE``.
_NOTHING = object()
#: How often a stream consumer re-checks its cancellation token while
#: waiting for the next chunk. Small enough to feel immediate, large enough
#: that the polling itself is negligible.
_CANCEL_POLL_SECONDS = 0.02


class RuntimeStream:
    """A cancellable, bounded streaming generation.

    Nothing runs until the stream is iterated.  Chunks arrive from a daemon
    producer thread through a bounded queue, so the consumer enforces both
    the overall deadline and a per-chunk timeout even against a backend that
    ignores its cancellation token.

    Iteration raises :class:`RuntimeTimeoutError` /
    :class:`RuntimeCancelledError` / :class:`BackendUnavailableError` on
    failure, and :attr:`response` is always populated afterwards, so a
    mid-stream failure can never be mistaken for a clean end of stream.
    """

    def __init__(self, runtime: "ModelRuntime", backend: ModelBackend,
                 request: RuntimeRequest, token: CancellationToken,
                 timeout: float, chunk_timeout: float) -> None:
        self.runtime = runtime
        self.backend = backend
        self.request = request
        self.token = token
        self.timeout = timeout
        self.chunk_timeout = chunk_timeout
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=64)
        self._response = RuntimeResponse(
            request_id=request.request_id, trace_id=request.trace_id,
            backend=backend.name, model=request.model)
        self._parts: List[str] = []
        self._chunks = 0
        self._consumed = False
        self._started_at = 0.0
        # Set once the consumer is done with the stream for any reason.  The
        # producer must observe this, or a backend that keeps emitting into a
        # full queue spins forever after the consumer raises: a leaked thread
        # per failed stream.  This is deliberately separate from ``token``
        # because ``_finalize`` reads ``token.cancelled`` to classify the
        # outcome, and stopping the producer is not a user cancellation.
        self._stopped = threading.Event()
        # Text the producer received from the backend but could not deliver
        # because the consumer had already gone.  Kept so a cancelled stream
        # reports what the backend actually produced instead of an empty
        # string, which would understate real output. Written only by the
        # producer thread and read only after that thread has been joined.
        self._undelivered: List[str] = []

    # -- control ----------------------------------------------------------

    def cancel(self, reason: str = CancelReason.USER.value) -> bool:
        return self.token.cancel(reason)

    @property
    def cancelled(self) -> bool:
        return self.token.cancelled

    @property
    def response(self) -> RuntimeResponse:
        """The structured result; meaningful once iteration has finished."""
        return self._response

    @property
    def text(self) -> str:
        return "".join(self._parts)

    # -- iteration --------------------------------------------------------

    def __iter__(self) -> Iterator[RuntimeChunk]:
        if self._consumed:
            raise ModelRuntimeError(
                "A RuntimeStream can only be consumed once "
                "(request_id={0}).".format(self.request.request_id))
        self._consumed = True
        return self._iterate()

    def _emit(self, item: Any) -> bool:
        """Queue one item, checking cancellation while the queue is full.

        Returns ``False`` when the item cannot be delivered, which is the
        producer's signal to stop reading from the backend.
        """
        while True:
            if self._stopped.is_set() or self.token.cancelled:
                return False
            try:
                self._queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue

    def _produce(self) -> None:
        try:
            source = self.backend.stream(self.request, self.token)
            try:
                chunks = iter(source)
            except TypeError:
                # A backend whose ``stream`` does not return an iterable has
                # broken the protocol; saying so is more useful than a
                # generic backend error, and it is a bug in the backend
                # rather than an outage.
                raise BackendProtocolError(
                    "Backend {0!r}.stream() returned {1}, which is not "
                    "iterable.".format(self.backend.name,
                                       type(source).__name__))
            for chunk in chunks:
                delivered = (not self._stopped.is_set()
                             and not self.token.cancelled
                             and self._emit(chunk))
                if not delivered:
                    # The consumer is gone, but the backend really did emit
                    # this text. Record it so the final response can report
                    # actual partial output rather than claiming there was
                    # none.
                    if isinstance(chunk, RuntimeChunk):
                        self._undelivered.append(chunk.text or "")
                    elif isinstance(chunk, str):
                        self._undelivered.append(chunk)
                    return
            self._emit(_DONE)
        except BaseException as exc:  # noqa: BLE001 - isolate the backend
            self._emit(exc)

    def _iterate(self) -> Iterator[RuntimeChunk]:
        self._started_at = time.perf_counter()
        self.runtime._begin_stream(self)
        worker = threading.Thread(target=self._produce,
                                  name="forge-runtime-stream", daemon=True)
        self._worker = worker
        worker.start()
        deadline = time.monotonic() + self.timeout
        failure: Optional[BaseException] = None
        try:
            while True:
                self.token.raise_if_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.token.cancel(CancelReason.TIMEOUT.value)
                    raise RuntimeTimeoutError(
                        "Streaming generation exceeded {0:.1f}s "
                        "(request_id={1}).".format(self.timeout,
                                                   self.request.request_id))
                # The wait is sliced rather than handed to a single
                # ``get(timeout=chunk_timeout)``: parked inside ``get`` the
                # consumer cannot see the token, so a cancellation would
                # only surface after the whole per-chunk bound elapsed and
                # would then be misreported as a timeout.
                chunk_deadline = (time.monotonic()
                                  + min(self.chunk_timeout, remaining))
                item = _NOTHING
                while True:
                    self.token.raise_if_cancelled()
                    wait = min(chunk_deadline, deadline) - time.monotonic()
                    if wait <= 0:
                        break
                    try:
                        item = self._queue.get(
                            timeout=min(wait, _CANCEL_POLL_SECONDS))
                        break
                    except queue.Empty:
                        continue
                if item is _NOTHING:
                    self.token.cancel(CancelReason.TIMEOUT.value)
                    if time.monotonic() >= deadline:
                        raise RuntimeTimeoutError(
                            "Streaming generation exceeded {0:.1f}s "
                            "(request_id={1}).".format(
                                self.timeout, self.request.request_id))
                    raise RuntimeTimeoutError(
                        "No streamed chunk arrived within {0:.1f}s "
                        "(request_id={1}).".format(self.chunk_timeout,
                                                   self.request.request_id))
                if item is _DONE:
                    break
                if isinstance(item, BaseException):
                    raise _wrap_backend_error(item)
                chunk = self._accept(item)
                self._chunks += 1
                yield chunk
            self._response.finish_reason = FinishReason.STOP.value
        except GeneratorExit:
            self.token.cancel(CancelReason.ABANDONED.value)
            failure = RuntimeCancelledError(
                "Stream abandoned by the consumer before completion.")
            raise
        except BaseException as exc:  # noqa: BLE001 - recorded then re-raised
            failure = exc
            raise
        finally:
            self._stopped.set()
            self._finalize(failure)
            self.runtime._end_stream(self)

    def _accept(self, item: Any) -> RuntimeChunk:
        if isinstance(item, RuntimeChunk):
            chunk = RuntimeChunk(
                text=item.text or "", index=self._chunks + 1,
                request_id=self.request.request_id,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                finish_reason=item.finish_reason,
                metadata=dict(item.metadata or {}))
        elif isinstance(item, str):
            chunk = RuntimeChunk(text=item, index=self._chunks + 1,
                                 request_id=self.request.request_id)
        else:
            raise BackendProtocolError(
                "Backend {0!r} streamed {1}; expected str or "
                "RuntimeChunk.".format(self.backend.name,
                                       type(item).__name__))
        self._parts.append(chunk.text)
        if chunk.input_tokens:
            self._response.input_tokens = chunk.input_tokens
        if chunk.output_tokens:
            self._response.output_tokens = chunk.output_tokens
        if chunk.finish_reason:
            self._response.finish_reason = chunk.finish_reason
        return chunk

    def _harvest_grace(self, failure: Optional[BaseException],
                       text: str) -> str:
        """Recover chunks the producer finished with during the grace window.

        Cancellation and the producer run concurrently, so a backend can
        legitimately complete while the consumer is unwinding.  Throwing
        that output away would report ``text=''`` for a run that really
        produced text.  The outcome stays ``cancelled`` — precedence is
        cancelled > timeout > success — but the words the backend actually
        emitted are kept and flagged as partial rather than fabricated.
        """
        if failure is None:
            return text
        worker = getattr(self, "_worker", None)
        if worker is None or worker is threading.current_thread():
            return text
        worker.join(CANCEL_GRACE_SECONDS)
        if worker.is_alive():
            return text
        drained = 0
        while drained < _GRACE_DRAIN_CHUNKS:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if item is _DONE or isinstance(item, BaseException):
                break
            try:
                self._accept(item)
                self._chunks += 1
            except BaseException:  # noqa: BLE001 - keep the outcome stable
                break
        # Anything the producer received but could not queue: the consumer
        # had already left, but the output is real and belongs in the report.
        for pending in list(self._undelivered):
            if pending:
                self._parts.append(pending)
        self._undelivered = []
        return "".join(self._parts)

    def _finalize(self, failure: Optional[BaseException]) -> None:
        text = "".join(self._parts)
        latency = (time.perf_counter() - self._started_at) * 1000.0
        self._response.text = text
        self._response.latency_ms = latency
        self._response.backend = self.backend.name
        if not self._response.model:
            self._response.model = self.request.model
        self._response.metadata["chunks"] = self._chunks
        text = self._harvest_grace(failure, text)
        self._response.text = text
        if failure is None:
            self._response.success = True
            self._response.error = ""
            self._response.error_kind = ErrorKind.NONE.value
            self._response.cancelled = False
            self._response.timed_out = False
            if not self._response.finish_reason:
                self._response.finish_reason = FinishReason.STOP.value
        else:
            kind = getattr(failure, "kind", ErrorKind.BACKEND)
            kind = kind.value if isinstance(kind, ErrorKind) else str(kind)
            self._response.success = False
            self._response.error = _error_text(failure)
            self._response.error_kind = kind
            self._response.finish_reason = (
                FinishReason.TIMEOUT.value
                if isinstance(failure, RuntimeTimeoutError)
                else FinishReason.CANCELLED.value
                if isinstance(failure, RuntimeCancelledError)
                else FinishReason.ERROR.value)
            self._response.timed_out = isinstance(failure, RuntimeTimeoutError)
            self._response.cancelled = (
                isinstance(failure, RuntimeCancelledError)
                or self.token.cancelled)
            if self._response.cancelled and not self._response.timed_out:
                self._response.finish_reason = FinishReason.CANCELLED.value
            if text:
                self._response.metadata["partial"] = True
        self.runtime._record_outcome(self.backend.name, self._response,
                                     streaming=True)


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------


@dataclass
class _InFlight:
    request_id: str
    backend: str
    model: str
    token: CancellationToken
    started_at: float = field(default_factory=time.time)
    streaming: bool = False
    stream: Optional[RuntimeStream] = None

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "request_id": self.request_id,
            "backend": self.backend,
            "model": self.model,
            "started_at": self.started_at,
            "streaming": self.streaming,
            "elapsed_ms": round((time.time() - self.started_at) * 1000.0, 3),
        }


class ModelRuntime:
    """First-party model execution runtime.

    Responsibilities: model discovery, model metadata, load/unload
    abstraction, generation, streaming generation, health checks, bounded
    timeouts, cancellation, and resource reporting — over pluggable
    backends selected explicitly by name.

    It is not a model and it is not the AI Engine.  It never invents
    output: an unavailable backend produces a failure the caller can act
    on.
    """

    def __init__(self, config: Optional[RuntimeConfig] = None,
                 backends: Optional[Sequence[ModelBackend]] = None) -> None:
        self.config = (config or RuntimeConfig()).validate()
        self._backends: Dict[str, ModelBackend] = {}
        self._models: Dict[str, RuntimeModel] = {}
        self._in_flight: Dict[str, _InFlight] = {}
        self._counters: Dict[str, Dict[str, int]] = {}
        self._history: "deque[Dict[str, Any]]" = deque(
            maxlen=max(0, int(self.config.history_size)))
        # Refusals that happened *before* any backend was reached (unknown
        # backend, duplicate request id, closed runtime). Kept apart from
        # ``_history`` on purpose: history and the per-backend counters
        # describe outcomes of generations that actually reached a backend,
        # and mixing in pre-backend refusals would conjure counter entries
        # for backends that do not exist. Separate, but not dropped.
        self._refusals: "deque[Dict[str, Any]]" = deque(
            maxlen=max(0, int(self.config.history_size)))
        self._lock = threading.RLock()
        self._closed = False
        self._created_at = time.time()
        for backend in backends or ():
            self.register_backend(backend)

    # -- lifecycle --------------------------------------------------------

    @classmethod
    def from_defaults(cls, config: Optional[RuntimeConfig] = None,
                      *, load_config: bool = True) -> "ModelRuntime":
        """Build a runtime from configuration. Never touches the network.

        Raises ``ValueError`` if the configured default backend is not one of
        the enabled built-ins, so a typo fails at construction instead of at
        the first request.
        """
        configuration = config or (RuntimeConfig.load() if load_config
                                   else RuntimeConfig())
        runtime = cls(configuration)
        for name in configuration.backends:
            runtime.register_backend(create_backend(name, configuration))
        if configuration.default_backend not in runtime._backends:
            raise ValueError(
                "default_backend {0!r} is not registered; enabled backends "
                "are {1}.".format(configuration.default_backend,
                                  list(configuration.backends)))
        return runtime

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Cancel in-flight work and refuse new requests. Idempotent."""
        with self._lock:
            self._closed = True
        self.cancel_all(CancelReason.SHUTDOWN.value)

    def _check_open(self) -> None:
        if self._closed:
            raise ModelRuntimeError("The model runtime is closed.")

    # -- backends ---------------------------------------------------------

    def register_backend(self, backend: ModelBackend,
                         replace: bool = False) -> ModelBackend:
        """Register a backend instance. Explicit registration only.

        The runtime never imports a backend from a string, so a custom
        backend must be constructed and handed over here by the host
        process.
        """
        if backend is None:
            raise BackendProtocolError("A backend instance is required.")
        validate = getattr(backend, "validate", None)
        if not callable(validate):
            raise BackendProtocolError(
                "{0!r} is not a model backend (no validate()).".format(
                    type(backend).__name__))
        validate()
        if not callable(getattr(backend, "generate", None)):
            raise BackendProtocolError(
                "Backend {0!r} does not implement generate().".format(
                    backend.name))
        with self._lock:
            if backend.name in self._backends and not replace:
                raise BackendProtocolError(
                    "Backend already registered: {0} (pass replace=True to "
                    "override explicitly).".format(backend.name))
            self._backends[backend.name] = backend
            self._counters.setdefault(backend.name, {
                "generations": 0, "failures": 0, "timeouts": 0,
                "cancellations": 0, "streams": 0})
        logger.debug("Registered model backend %s (kind=%s)", backend.name,
                     getattr(backend, "kind", "custom"))
        return backend

    def unregister_backend(self, name: str) -> bool:
        with self._lock:
            removed = self._backends.pop(name, None)
            self._counters.pop(name, None)
            for model_id in [key for key, model in self._models.items()
                             if model.backend == name]:
                self._models.pop(model_id, None)
        return removed is not None

    def has_backend(self, name: str) -> bool:
        with self._lock:
            return name in self._backends

    def get_backend(self, name: str) -> ModelBackend:
        with self._lock:
            try:
                return self._backends[name]
            except KeyError:
                raise BackendNotFoundError(
                    "Unknown backend {0!r}. Registered backends: {1}".format(
                        name, ", ".join(sorted(self._backends)) or "(none)")) \
                    from None

    def select_backend(self, name: str = "") -> ModelBackend:
        """Resolve the backend for a request.

        Selection is explicit: an empty name means the *configured default*,
        never "any backend that answers".  There is no cross-backend
        failover inside the runtime; choosing another backend is a routing
        decision that belongs to the caller.
        """
        chosen = str(name or "").strip() or self.config.default_backend
        return self.get_backend(chosen)

    def backends(self) -> List[RuntimeBackendInfo]:
        with self._lock:
            instances = list(self._backends.values())
        infos: List[RuntimeBackendInfo] = []
        for backend in instances:
            try:
                infos.append(backend.info())
            except Exception as exc:
                infos.append(RuntimeBackendInfo(
                    name=backend.name,
                    kind=getattr(backend, "kind", BackendKind.CUSTOM.value),
                    local=bool(getattr(backend, "local", True)),
                    requires_network=bool(
                        getattr(backend, "requires_network", False)),
                    available=False,
                    detail="Backend info failed: {0}".format(_error_text(exc))))
        infos.sort(key=lambda info: info.name)
        return infos

    # -- discovery / metadata --------------------------------------------

    def discover(self, backend: str = "", *, refresh: bool = True
                 ) -> Dict[str, Any]:
        """Discover models from one backend (or every registered one).

        One failing backend never hides the others: per-backend errors are
        reported instead of aborting the sweep.  ``refresh=False`` reports
        what is already known without asking any backend again.
        """
        self._check_open()
        names = ([backend] if backend
                 else sorted(self._backends.keys()))
        results: Dict[str, Any] = {}
        for name in names:
            try:
                instance = self.get_backend(name)
            except BackendNotFoundError as exc:
                results[name] = {"discovered": [], "error": str(exc)}
                continue
            if not refresh:
                with self._lock:
                    known = [key for key in sorted(self._models)
                             if self._models[key].backend == name]
                results[name] = {"discovered": known, "refreshed": False}
                continue
            try:
                found = instance.list_models()
            except Exception as exc:
                results[name] = {"discovered": [],
                                 "error": _error_text(exc)}
                continue
            registered: List[str] = []
            with self._lock:
                for model in found:
                    if not isinstance(model, RuntimeModel):
                        continue
                    if not model.backend:
                        model.backend = name
                    if not model.model_id:
                        model.model_id = RuntimeModel.make_id(name, model.name)
                    previous = self._models.get(model.model_id)
                    if previous is not None and previous.loaded:
                        model.loaded = True
                    self._models[model.model_id] = model
                    registered.append(model.model_id)
            results[name] = {"discovered": registered, "refreshed": True}
        return results

    def models(self, backend: str = "", capability: str = "",
               *, loaded_only: bool = False) -> List[RuntimeModel]:
        """Known models, optionally filtered. Empty until discovered."""
        with self._lock:
            items = list(self._models.values())
        if backend:
            items = [model for model in items if model.backend == backend]
        if capability:
            items = [model for model in items
                     if capability in model.capabilities]
        if loaded_only:
            items = [model for model in items if model.loaded]
        items.sort(key=lambda model: model.model_id)
        return items

    def get_model(self, model_id: str) -> RuntimeModel:
        with self._lock:
            try:
                return self._models[model_id]
            except KeyError:
                raise ModelNotFoundError(
                    "Unknown model {0!r}. Known models: {1}".format(
                        model_id,
                        ", ".join(sorted(self._models)) or "(none - run "
                        "discovery first)")) from None

    def resolve_model(self, name: str, backend: str = "") -> RuntimeModel:
        """Resolve ``"<backend>:<name>"`` or a bare name within a backend."""
        wanted = str(name or "").strip()
        if not wanted:
            raise ModelNotFoundError("A model id or name is required.")
        with self._lock:
            if wanted in self._models:
                return self._models[wanted]
            matches = [model for key, model in self._models.items()
                       if model.name == wanted
                       and (not backend or model.backend == backend)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ModelNotFoundError(
                "Model name {0!r} is ambiguous across backends ({1}); use "
                "'<backend>:<name>'.".format(
                    wanted, ", ".join(sorted(m.model_id for m in matches))))
        raise ModelNotFoundError(
            "Unknown model {0!r}. Run discovery first.".format(wanted))

    def load(self, model_id: str, backend: str = "",
             timeout: Optional[float] = None) -> RuntimeModel:
        """Load a model through its backend's loading abstraction."""
        self._check_open()
        model = self.resolve_model(model_id, backend)
        instance = self.get_backend(model.backend)
        token = CancellationToken()
        started = time.perf_counter()
        try:
            loaded = instance.load_model(model, token)
        except Exception as exc:
            raise BackendUnavailableError(
                "Could not load {0}: {1}".format(model.model_id,
                                                 _error_text(exc))) from exc
        if not isinstance(loaded, RuntimeModel):
            raise BackendProtocolError(
                "Backend {0!r} returned {1} from load_model(); expected "
                "RuntimeModel.".format(instance.name, type(loaded).__name__))
        loaded.loaded = True
        with self._lock:
            self._models[loaded.model_id] = loaded
        logger.debug("Loaded model %s via %s in %.1fms", loaded.model_id,
                     instance.name, (time.perf_counter() - started) * 1000.0)
        return loaded

    def unload(self, model_id: str, backend: str = "") -> bool:
        """Release a model through its backend's unload abstraction."""
        self._check_open()
        model = self.resolve_model(model_id, backend)
        instance = self.get_backend(model.backend)
        try:
            released = bool(instance.unload_model(model.model_id))
        except Exception as exc:
            raise BackendUnavailableError(
                "Could not unload {0}: {1}".format(model.model_id,
                                                   _error_text(exc))) from exc
        with self._lock:
            known = self._models.get(model.model_id)
            if known is not None:
                known.loaded = False
        logger.debug("Unloaded model %s via %s", model.model_id,
                     instance.name)
        return released

    # -- inference --------------------------------------------------------

    def generate(self, request: RuntimeRequest) -> RuntimeResponse:
        """Run one bounded, cancellable generation.

        Never raises for operational failures: the result is a
        :class:`RuntimeResponse` with ``success=False`` and an
        ``error_kind``.  A :class:`ModelRuntimeError` escapes only for
        programmer errors (a ``None`` request).
        """
        if not isinstance(request, RuntimeRequest):
            raise ModelRuntimeError(
                "generate() requires a RuntimeRequest, got {0}.".format(
                    type(request).__name__))
        started = time.perf_counter()
        if self._closed:
            return self._refuse(request, request.backend, ErrorKind.CLOSED,
                                "The model runtime is closed.", started)

        try:
            backend = self.select_backend(request.backend)
        except BackendNotFoundError as exc:
            return self._refuse(request, request.backend, ErrorKind.NOT_FOUND,
                                str(exc), started)

        timeout = self.config.clamp_timeout(request.timeout)
        token = CancellationToken()
        with self._lock:
            if request.request_id in self._in_flight:
                conflict = self._refuse(
                    request, backend.name, ErrorKind.CONFLICT,
                    "Request id {0!r} is already in flight.".format(
                        request.request_id), started)
                # Return outside the lock: _refuse takes it again via
                # _finish, and the RLock would mask a real deadlock.
                return conflict
            self._in_flight[request.request_id] = _InFlight(
                request_id=request.request_id, backend=backend.name,
                model=request.model, token=token)

        logger.debug("Runtime generate backend=%s model=%s request=%s "
                     "timeout=%.1fs prompt_chars=%d", backend.name,
                     request.model or "-", request.request_id, timeout,
                     len(request.prompt or ""))

        holder: Dict[str, Any] = {}
        attempts_allowed = 1 + max(0, int(
            request.retries if request.retries is not None
            else self.config.retries))

        def _work() -> None:
            """Call the backend, retrying bounded transient failures.

            The whole retry sequence lives inside the worker so the single
            configured ``timeout`` still bounds it end to end, and the
            in-flight/cancellation bookkeeping around the worker is
            unchanged. Only retryable kinds are retried, the backoff is
            exponential and capped, and every sleep is interruptible.
            """
            deadline = time.monotonic() + timeout
            attempt = 0
            last_error: Optional[BaseException] = None
            while True:
                attempt += 1
                try:
                    response = backend.generate(request, token)
                except BaseException as exc:  # noqa: BLE001 - isolate backend
                    # Held in a local, not in ``holder``: ``generate`` treats
                    # the presence of ``holder["error"]`` as the final
                    # outcome, so publishing an intermediate failure would
                    # shadow a later successful attempt.
                    last_error = exc
                    kind = getattr(exc, "kind", None)
                    kind = kind.value if isinstance(kind, ErrorKind) else kind
                    retryable = (not token.cancelled
                                 and attempt < attempts_allowed
                                 and kind in RETRYABLE_ERROR_KINDS
                                 and not isinstance(exc, NEVER_RETRY_ERRORS))
                    if not retryable:
                        break
                    backoff = min(
                        MAX_RETRY_BACKOFF_SECONDS,
                        max(0.0, self.config.retry_backoff_seconds)
                        * (2 ** (attempt - 1)))
                    logger.debug(
                        "Runtime retry backend=%s request=%s attempt=%d/%d "
                        "kind=%s backoff=%.3fs", backend.name,
                        request.request_id, attempt, attempts_allowed, kind,
                        backoff)
                    if not self._retry_sleep(token, backoff, deadline):
                        break
                    continue
                holder["attempts"] = attempt
                holder["response"] = response
                return
            holder["attempts"] = attempt
            if last_error is not None:
                holder["error"] = last_error

        worker = threading.Thread(target=_work, name="forge-runtime-generate",
                                  daemon=True)
        worker.start()
        self._await_worker(worker, token, timeout)

        # Outcome precedence is explicit and total: a request the caller
        # cancelled is reported as cancelled (never as a timeout or a
        # success), a request whose deadline expired is reported as a
        # timeout, and only an uncancelled, completed backend call can
        # succeed.  Whatever text a backend did produce is preserved and
        # flagged partial rather than silently dropped or presented as
        # complete.
        user_cancelled = (token.cancelled
                          and token.reason != CancelReason.TIMEOUT.value)

        if worker.is_alive():
            # The backend ignored the token, or the deadline expired first.
            # Either way the runtime stops waiting: the worker is a daemon,
            # so it can never block shutdown, and the result is abandoned.
            token.cancel(CancelReason.TIMEOUT.value)
            worker.join(CANCEL_GRACE_SECONDS)
            if user_cancelled:
                response = self._cancelled_response(
                    request, backend.name, token.reason, started, text="")
            else:
                response = RuntimeResponse(
                    text="", model=request.model, backend=backend.name,
                    success=False,
                    error=("Generation exceeded the {0:.1f}s bound."
                           .format(timeout)),
                    error_kind=ErrorKind.TIMEOUT.value, timed_out=True,
                    request_id=request.request_id, trace_id=request.trace_id,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    finish_reason=FinishReason.TIMEOUT.value)
            self._finish(request, response,
                             holder.get("attempts", 1))
            return response

        if user_cancelled:
            partial = holder.get("response")
            text = partial.text if isinstance(partial, RuntimeResponse) else ""
            response = self._cancelled_response(
                request, backend.name, token.reason, started, text=text or "")
            self._finish(request, response,
                             holder.get("attempts", 1))
            return response

        if "error" in holder:
            exc = holder["error"]
            kind = getattr(exc, "kind", ErrorKind.BACKEND)
            kind = kind.value if isinstance(kind, ErrorKind) else str(kind)
            response = RuntimeResponse.failure(
                _error_text(exc), kind=kind, backend=backend.name,
                model=request.model, request_id=request.request_id,
                trace_id=request.trace_id,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                cancelled=bool(token.cancelled),
                timed_out=isinstance(exc, RuntimeTimeoutError))
            self._finish(request, response,
                             holder.get("attempts", 1))
            return response

        result = holder.get("response")
        if not isinstance(result, RuntimeResponse):
            response = RuntimeResponse.failure(
                "Backend {0!r} returned {1} from generate(); expected "
                "RuntimeResponse.".format(backend.name,
                                          type(result).__name__),
                kind=ErrorKind.PROTOCOL.value, backend=backend.name,
                model=request.model, request_id=request.request_id,
                trace_id=request.trace_id,
                latency_ms=(time.perf_counter() - started) * 1000.0)
            self._finish(request, response,
                             holder.get("attempts", 1))
            return response

        result.request_id = request.request_id
        result.trace_id = request.trace_id or result.trace_id
        result.backend = backend.name
        if not result.model:
            result.model = request.model
        if not result.latency_ms:
            result.latency_ms = (time.perf_counter() - started) * 1000.0
        result.error = redact_text(result.error or "")
        # Normalise a backend that reports failure without saying how, so
        # callers can always branch on error_kind and finish_reason.
        if not result.success:
            if not result.error_kind:
                result.error_kind = ErrorKind.BACKEND.value
            if result.finish_reason == FinishReason.STOP.value:
                result.finish_reason = FinishReason.ERROR.value
        self._finish(request, result,
                             holder.get("attempts", 1))
        return result

    def stream(self, request: RuntimeRequest) -> RuntimeStream:
        """Start a cancellable streaming generation (lazy).

        Returns a :class:`RuntimeStream`; nothing is sent to the backend
        until the stream is iterated.
        """
        if not isinstance(request, RuntimeRequest):
            raise ModelRuntimeError(
                "stream() requires a RuntimeRequest, got {0}.".format(
                    type(request).__name__))
        if self._closed:
            raise ModelRuntimeError("The model runtime is closed.")
        backend = self.select_backend(request.backend)
        token = CancellationToken()
        timeout = self.config.clamp_timeout(request.timeout)
        chunk_timeout = min(self.config.chunk_timeout_seconds, timeout)
        stream = RuntimeStream(self, backend, request, token, timeout,
                               chunk_timeout)
        logger.debug("Runtime stream backend=%s model=%s request=%s "
                     "timeout=%.1fs", backend.name, request.model or "-",
                     request.request_id, timeout)
        return stream

    def _begin_stream(self, stream: RuntimeStream) -> None:
        request = stream.request
        with self._lock:
            if request.request_id in self._in_flight:
                raise ModelRuntimeError(
                    "Request id {0!r} is already in flight.".format(
                        request.request_id))
            self._in_flight[request.request_id] = _InFlight(
                request_id=request.request_id, backend=stream.backend.name,
                model=request.model, token=stream.token, streaming=True,
                stream=stream)
            counters = self._counters.setdefault(stream.backend.name, {
                "generations": 0, "failures": 0, "timeouts": 0,
                "cancellations": 0, "streams": 0})
            counters["streams"] = counters.get("streams", 0) + 1

    def _end_stream(self, stream: RuntimeStream) -> None:
        with self._lock:
            self._in_flight.pop(stream.request.request_id, None)

    def _refuse(self, request: RuntimeRequest, backend: str,
                kind: ErrorKind, message: str,
                started: float) -> RuntimeResponse:
        """Build *and record* a refusal that happens before any backend call.

        These paths used to return a failure without recording it, so a
        duplicate request id, an unknown backend or a closed runtime was
        invisible to :meth:`history` and :meth:`metrics` — telemetry
        silently under-reported failures it had actually refused.

        Only refusals that a *registered* backend is responsible for are
        charged to that backend's lifetime counters; an unknown backend name
        is recorded as an event but must not conjure a counter entry for a
        backend that does not exist.
        """
        response = RuntimeResponse.failure(
            message, kind=kind.value, backend=backend, model=request.model,
            request_id=request.request_id, trace_id=request.trace_id,
            latency_ms=(time.perf_counter() - started) * 1000.0)
        response.metadata["attempts"] = 1
        # Deliberately not ``_finish``: a refusal never inserted an in-flight
        # entry, and a conflict shares its request_id with the request that
        # is genuinely in flight. Popping here would evict that live entry
        # and make the original request uncancellable. It is also kept out of
        # ``_history`` and the per-backend counters, which describe
        # generations that reached a backend; refusals get their own bounded
        # log so they are still visible instead of silently vanishing.
        with self._lock:
            self._refusals.append({
                "at": time.time(),
                "request_id": request.request_id,
                "trace_id": request.trace_id,
                "backend": backend or request.backend,
                "model": request.model,
                "error_kind": kind.value,
                "error": response.error,
            })
        logger.debug("Runtime refusal backend=%s kind=%s request=%s",
                     backend or request.backend or "-", kind.value,
                     request.request_id)
        return response

    def refusals(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Pre-backend refusals, newest first.

        These are requests the runtime turned away before reaching any
        backend — an unknown backend name, a duplicate in-flight request id,
        or a call after :meth:`close`.  They are deliberately absent from
        :meth:`history`, which reports outcomes of generations that did
        reach a backend, so this is where they can be seen instead.
        """
        with self._lock:
            items = list(self._refusals)
        items.reverse()
        return items[: max(0, int(limit))]

    def _finish(self, request: RuntimeRequest,
                response: RuntimeResponse,
                attempts: int = 1) -> RuntimeResponse:
        """Release the in-flight slot and record the outcome.

        ``attempts`` is recorded so a retried call is never mistaken for a
        first-try success — an operator reading telemetry can see that a
        response needed three tries to arrive.
        """
        response.metadata["attempts"] = max(1, int(attempts or 1))
        with self._lock:
            self._in_flight.pop(request.request_id, None)
        self._record_outcome(response.backend or request.backend, response)
        return response

    @staticmethod
    def _cancelled_response(request: RuntimeRequest, backend: str,
                            reason: str, started: float,
                            text: str = "") -> RuntimeResponse:
        """Build the failure returned for a cancelled generation."""
        response = RuntimeResponse(
            text=text, model=request.model, backend=backend, success=False,
            error="Generation cancelled ({0}).".format(reason or "cancelled"),
            error_kind=ErrorKind.CANCELLED.value, cancelled=True,
            request_id=request.request_id, trace_id=request.trace_id,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            finish_reason=FinishReason.CANCELLED.value)
        if text:
            response.metadata["partial"] = True
        return response

    @staticmethod
    def _retry_sleep(token: CancellationToken, seconds: float,
                     deadline: float) -> bool:
        """Sleep before a retry, staying responsive to cancellation.

        Returns ``False`` if the wait was cut short by cancellation or by the
        request deadline, which means "do not retry".
        """
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if token.cancelled:
                return False
            remaining = min(end, deadline) - time.monotonic()
            if remaining <= 0:
                return time.monotonic() < end
            time.sleep(min(remaining, 0.05))
            if time.monotonic() >= end:
                return not token.cancelled

    @staticmethod
    def _await_worker(worker: threading.Thread, token: CancellationToken,
                      timeout: float) -> None:
        """Wait for a backend worker while staying responsive to cancellation.

        A bare ``Thread.join(timeout)`` would report a *timeout* even when the
        caller cancelled a second into a bounded minute, so the wait is
        sliced: the worker is joined in short steps and the token is checked
        between them.  Normal completion is still detected immediately,
        because ``join`` returns as soon as the thread ends.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while not token.cancelled:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            worker.join(min(remaining, 0.01))
            if not worker.is_alive():
                return

    # -- cancellation -----------------------------------------------------

    def cancel(self, request_id: str,
               reason: str = CancelReason.USER.value) -> bool:
        """Cancel one in-flight request. Returns ``True`` if it was running."""
        with self._lock:
            entry = self._in_flight.get(request_id)
        if entry is None:
            return False
        return entry.token.cancel(reason)

    def cancel_all(self, reason: str = CancelReason.SHUTDOWN.value) -> int:
        """Cancel every in-flight request (used by :meth:`close`)."""
        with self._lock:
            entries = list(self._in_flight.values())
        cancelled = 0
        for entry in entries:
            if entry.token.cancel(reason):
                cancelled += 1
        return cancelled

    def in_flight(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [entry.to_dict()
                    for entry in sorted(self._in_flight.values(),
                                        key=lambda item: item.started_at)]

    # -- health / resources / status -------------------------------------

    def health(self, backend: str = "", *,
               probe: bool = True) -> List[RuntimeHealth]:
        """Health for one backend or every registered backend.

        A probe never raises: an unreachable backend is reported as
        ``unavailable`` with a redacted error, and a backend that needs the
        network is reported unavailable without being contacted when
        network access is disabled.
        """
        names = ([backend] if backend else sorted(self._backends.keys()))
        results: List[RuntimeHealth] = []
        for name in names:
            try:
                instance = self.get_backend(name)
            except BackendNotFoundError as exc:
                results.append(RuntimeHealth(
                    backend=name, status=RuntimeState.UNAVAILABLE.value,
                    checked_at=time.time(), error=str(exc),
                    detail="Backend is not registered."))
                continue
            try:
                reported = instance.health(probe=probe)
            except Exception as exc:
                reported = RuntimeHealth(
                    backend=name,
                    kind=getattr(instance, "kind",
                                 BackendKind.CUSTOM.value),
                    status=RuntimeState.UNAVAILABLE.value,
                    checked_at=time.time(), error=_error_text(exc),
                    detail="Health probe failed.")
            if not isinstance(reported, RuntimeHealth):
                reported = RuntimeHealth(
                    backend=name, status=RuntimeState.UNAVAILABLE.value,
                    checked_at=time.time(),
                    detail="Backend returned an invalid health report.")
            reported.backend = name
            reported.requires_network = bool(
                getattr(instance, "requires_network", False))
            reported.network_allowed = bool(self.config.allow_network)
            reported.probed = bool(probe)
            if not reported.checked_at:
                reported.checked_at = time.time()
            with self._lock:
                counters = dict(self._counters.get(name, {}))
                models = [model for model in self._models.values()
                          if model.backend == name]
            reported.generations = int(counters.get("generations", 0))
            reported.failures = int(counters.get("failures", 0))
            reported.timeouts = int(counters.get("timeouts", 0))
            reported.cancellations = int(counters.get("cancellations", 0))
            if not reported.models_available:
                reported.models_available = len(models)
            reported.models_loaded = len(
                [model for model in models if model.loaded])
            results.append(reported)
        return results

    def resources(self) -> RuntimeResources:
        """Measured host resources plus runtime occupancy."""
        host = system_resources()
        with self._lock:
            models = list(self._models.values())
            in_flight = len(self._in_flight)
            backend_names = list(self._backends)
        accelerators: List[str] = []
        extra: Dict[str, Any] = {}
        for name in backend_names:
            try:
                reported = self._backends[name].resources()
            except Exception:
                continue
            if not isinstance(reported, dict):
                continue
            for accelerator in reported.get("accelerators", []) or []:
                if accelerator not in accelerators:
                    accelerators.append(str(accelerator))
            extra[name] = redact(dict(reported))
        return RuntimeResources(
            cpu_count=int(host.get("cpu_count", 0)),
            memory_total_bytes=int(host.get("memory_total_bytes", 0)),
            memory_available_bytes=int(host.get("memory_available_bytes", 0)),
            platform=str(host.get("platform", "")),
            python_version=str(host.get("python_version", "")),
            models_known=len(models),
            models_loaded=len([model for model in models if model.loaded]),
            in_flight=in_flight,
            backends=len(backend_names),
            accelerators=tuple(accelerators),
            extra=extra,
        )

    def status(self, *, probe: bool = False) -> Dict[str, Any]:
        """One structured snapshot of the runtime (no content, no secrets)."""
        with self._lock:
            models = list(self._models.values())
            counters = {name: dict(values)
                        for name, values in self._counters.items()}
        by_backend: Dict[str, int] = {}
        for model in models:
            by_backend[model.backend] = by_backend.get(model.backend, 0) + 1
        return {
            "runtime": {
                "version": RUNTIME_VERSION,
                "closed": self._closed,
                "created_at": self._created_at,
                "config": self.config.to_dict(),
            },
            "backends": [info.to_dict() for info in self.backends()],
            "health": [item.to_dict() for item in self.health(probe=probe)],
            "models": {
                "total": len(models),
                "loaded": len([model for model in models if model.loaded]),
                "by_backend": by_backend,
            },
            "resources": self.resources().to_dict(),
            "counters": counters,
            "in_flight": self.in_flight(),
        }

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Recent outcomes, newest first: identifiers and counters only.

        No prompt, context, or completion text is ever recorded — only sizes.
        """
        with self._lock:
            items = list(self._history)
        items.reverse()
        return items[: max(0, int(limit))]

    def metrics(self, backend: str = "") -> Dict[str, Any]:
        """Latency and reliability metrics for one backend or all of them.

        Computed from the recorded outcome history, which is bounded by
        ``RuntimeConfig.history_size`` — so these are *windowed* figures over
        the recent past, not lifetime totals.  Percentiles come from the
        actual observed latencies; nothing is modelled, smoothed, or
        invented, and an empty window reports ``None`` rather than a
        fabricated zero that would look like "instant".

        Contains identifiers and aggregates only, never prompt or completion
        text, matching :meth:`history`.
        """
        with self._lock:
            items = list(self._history)
            refusals = list(self._refusals)
            counters = {name: dict(values)
                        for name, values in self._counters.items()}
        if backend:
            items = [item for item in items if item.get("backend") == backend]
            refusals = [item for item in refusals
                        if item.get("backend") == backend]

        def _percentile(values: List[float], pct: float) -> Optional[float]:
            if not values:
                return None
            ordered = sorted(values)
            if len(ordered) == 1:
                return round(ordered[0], 3)
            # Nearest-rank with linear interpolation between the two
            # surrounding samples: exact at both ends and monotonic.
            position = (len(ordered) - 1) * (max(0.0, min(100.0, pct)) / 100.0)
            lower = int(position)
            upper = min(lower + 1, len(ordered) - 1)
            weight = position - lower
            return round(ordered[lower] + (ordered[upper] - ordered[lower])
                         * weight, 3)

        total = len(items)
        successes = sum(1 for item in items if item.get("success"))
        failures = total - successes
        latencies = [float(item.get("latency_ms") or 0.0) for item in items]
        error_kinds: Dict[str, int] = {}
        for item in items:
            if item.get("success"):
                continue
            key = str(item.get("error_kind") or "unknown")
            error_kinds[key] = error_kinds.get(key, 0) + 1
        attempted = [int(item.get("attempts") or 1) for item in items]
        retried = sum(1 for count in attempted if count > 1)

        by_backend: Dict[str, Any] = {}
        for name in sorted({str(item.get("backend") or "") for item in items}
                           | set(counters)):
            scoped = [item for item in items
                      if str(item.get("backend") or "") == name]
            scoped_latency = [float(item.get("latency_ms") or 0.0)
                              for item in scoped]
            scoped_success = sum(1 for item in scoped
                                 if item.get("success"))
            recorded = counters.get(name, {})
            by_backend[name] = {
                "requests": len(scoped),
                "successes": scoped_success,
                "failures": len(scoped) - scoped_success,
                "success_rate": (round(scoped_success / len(scoped), 4)
                                 if scoped else None),
                "latency_ms": {
                    "p50": _percentile(scoped_latency, 50.0),
                    "p95": _percentile(scoped_latency, 95.0),
                    "p99": _percentile(scoped_latency, 99.0),
                    "min": round(min(scoped_latency), 3) if scoped_latency
                    else None,
                    "max": round(max(scoped_latency), 3) if scoped_latency
                    else None,
                },
                # Lifetime counters survive history truncation, so they can
                # exceed the windowed figures above; they are labelled as
                # such rather than being passed off as window stats.
                "lifetime": {
                    "generations": int(recorded.get("generations", 0)),
                    "failures": int(recorded.get("failures", 0)),
                    "timeouts": int(recorded.get("timeouts", 0)),
                    "cancellations": int(recorded.get("cancellations", 0)),
                    "streams": int(recorded.get("streams", 0)),
                },
            }

        refused_kinds: Dict[str, int] = {}
        for item in refusals:
            key = str(item.get("error_kind") or "unknown")
            refused_kinds[key] = refused_kinds.get(key, 0) + 1

        return {
            "window_size": int(self.config.history_size),
            "backend": backend,
            "requests": total,
            "successes": successes,
            "failures": failures,
            "success_rate": round(successes / total, 4) if total else None,
            "retried_requests": retried,
            "retry_attempts": sum(attempted),
            # Refusals are reported apart from ``requests`` because they
            # never reached a backend; folding them into the success rate
            # would describe backend reliability in terms of routing errors.
            "refused_requests": len(refusals),
            "refusals": dict(sorted(refused_kinds.items())),
            "latency_ms": {
                "p50": _percentile(latencies, 50.0),
                "p95": _percentile(latencies, 95.0),
                "p99": _percentile(latencies, 99.0),
                "min": round(min(latencies), 3) if latencies else None,
                "max": round(max(latencies), 3) if latencies else None,
            },
            "error_kinds": dict(sorted(error_kinds.items())),
            "by_backend": by_backend,
            "in_flight": len(self.in_flight()),
        }

    # -- internals --------------------------------------------------------

    def _record_outcome(self, backend: str, response: RuntimeResponse,
                        *, streaming: bool = False) -> None:
        """Charge one generation outcome to a backend and the history window.

        Only generations that actually reached a backend belong here;
        pre-backend refusals are logged separately by :meth:`_refuse` so
        that ``_counters`` stays keyed by registered backends alone.
        """
        with self._lock:
            counters = self._counters.setdefault(backend, {
                "generations": 0, "failures": 0, "timeouts": 0,
                "cancellations": 0, "streams": 0})
            counters["generations"] = counters.get("generations", 0) + 1
            if not response.success:
                counters["failures"] = counters.get("failures", 0) + 1
            if response.timed_out:
                counters["timeouts"] = counters.get("timeouts", 0) + 1
            if response.cancelled:
                counters["cancellations"] = (
                    counters.get("cancellations", 0) + 1)
            self._history.append({
                "at": time.time(),
                "request_id": response.request_id,
                "trace_id": response.trace_id,
                "backend": backend,
                "model": response.model,
                "success": bool(response.success),
                "error_kind": response.error_kind,
                "error": response.error,
                "streaming": bool(streaming),
                "text_chars": len(response.text or ""),
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "latency_ms": round(float(response.latency_ms or 0.0), 3),
                "finish_reason": response.finish_reason,
                "attempts": int(response.metadata.get("attempts") or 1),
            })
        logger.debug("Runtime outcome backend=%s success=%s kind=%s "
                     "latency=%.1fms", backend, response.success,
                     response.error_kind or "-", response.latency_ms)


def create_default_runtime(config: Optional[RuntimeConfig] = None,
                           *, load_config: bool = True) -> ModelRuntime:
    """Convenience constructor used by the CLI and the desktop app."""
    return ModelRuntime.from_defaults(config, load_config=load_config)
