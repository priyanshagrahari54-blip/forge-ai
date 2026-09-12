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
                    Sequence, Tuple)
from uuid import uuid4

__all__ = [
    "ALLOWED_MODEL_EXTENSIONS",
    "BUILTIN_BACKENDS",
    "EXECUTABLE_EXTENSIONS",
    "PICKLE_EXTENSIONS",
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
#: Largest header this module will read when parsing model metadata.
MAX_HEADER_BYTES = 8 * 1024 * 1024
#: Largest file discovery will even look at (8 GiB); a model artifact is big,
#: but this bounds a hostile directory of sparse files.
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024

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
    """A redacted, single-line description of an exception."""
    message = str(exc) or exc.__class__.__name__
    return redact_text(" ".join(message.split()))[:500]


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
        if "@" in self.ollama_url:
            raise ValueError(
                "ollama_url must not embed credentials (use no userinfo)")
        return self

    def clamp_timeout(self, timeout: Optional[float]) -> float:
        """Clamp a requested timeout into the configured bounds."""
        requested = self.timeout_seconds if timeout is None else float(timeout)
        if requested <= 0:
            requested = self.timeout_seconds
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

        backends = (_tuple(data.get("backends"))
                    or _tuple(env.get("FORGE_RUNTIME_BACKENDS"))
                    or ("native",))
        default_backend = str(data.get("default_backend")
                              or env.get("FORGE_RUNTIME_BACKEND")
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
            ollama_url=str(data.get("ollama_url")
                           or env.get("FORGE_RUNTIME_OLLAMA_URL")
                           or env.get("OLLAMA_BASE_URL")
                           or env.get("OLLAMA_URL")
                           or "http://127.0.0.1:11434"),
            ollama_model=str(data.get("ollama_model")
                             or env.get("FORGE_RUNTIME_OLLAMA_MODEL")
                             or env.get("OLLAMA_MODEL") or ""),
            model_dirs=model_dirs,
            timeout_seconds=float(data.get("timeout_seconds")
                                  or env.get("FORGE_RUNTIME_TIMEOUT")
                                  or DEFAULT_TIMEOUT_SECONDS),
            max_timeout_seconds=float(
                data.get("max_timeout_seconds")
                or env.get("FORGE_RUNTIME_MAX_TIMEOUT")
                or MAX_TIMEOUT_SECONDS),
            chunk_timeout_seconds=float(
                data.get("chunk_timeout_seconds")
                or DEFAULT_CHUNK_TIMEOUT_SECONDS),
            health_timeout_seconds=float(
                data.get("health_timeout_seconds")
                or DEFAULT_HEALTH_TIMEOUT_SECONDS),
            history_size=int(data.get("history_size") or 200),
        )
        return config.validate()

    @classmethod
    def load(cls, path: Optional[str] = None, *,
             env: Optional[Mapping[str, str]] = None) -> "RuntimeConfig":
        """Load configuration from a file layered over the environment.

        Looks for ``.forge/runtime.yaml`` then ``.forge/runtime.json`` by
        default.  A missing file is not an error: environment defaults
        apply, and the default posture is offline with the native backend.
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
            except OSError:
                continue
            if candidate.endswith(".json"):
                try:
                    loaded = json.loads(text)
                except ValueError:
                    continue
                if isinstance(loaded, dict):
                    data.update(loaded)
                continue
            try:
                import yaml  # type: ignore
            except ImportError:
                continue
            try:
                loaded = yaml.safe_load(text) or {}
            except Exception:
                continue
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


def describe_gguf(path: str) -> Dict[str, Any]:
    """Read the real GGUF header (magic, version, tensor/metadata counts)."""
    header = _read_header(path, 64)
    if len(header) < 24 or header[:4] != b"GGUF":
        return {"format": "gguf", "header_ok": False}
    try:
        version, tensor_count, kv_count = struct.unpack("<IQQ", header[4:24])
    except struct.error:
        return {"format": "gguf", "header_ok": False}
    return {
        "format": "gguf",
        "header_ok": True,
        "gguf_version": int(version),
        "tensor_count": int(tensor_count),
        "metadata_entries": int(kv_count),
    }


def describe_safetensors(path: str) -> Dict[str, Any]:
    """Read the real safetensors JSON header (bounded)."""
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
    return {
        "format": "safetensors",
        "header_ok": True,
        "tensor_count": max(0, len(parsed) - (1 if "__metadata__" in parsed
                                              else 0)),
        "header_bytes": int(length),
        "declared_format": str(metadata.get("format", ""))[:64],
        "declared_architecture": str(
            metadata.get("architecture",
                         metadata.get("model_type", "")))[:128],
    }


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
                 adapter: Optional[InferenceAdapter] = None) -> None:
        self.model_dirs: Tuple[str, ...] = tuple(model_dirs or ())
        self.extensions: Tuple[str, ...] = tuple(
            ext.lower() for ext in (extensions or ALLOWED_MODEL_EXTENSIONS))
        self.max_depth = max(1, int(max_depth))
        self.max_files = max(1, int(max_files))
        self.adapter = adapter
        self._loaded: Dict[str, RuntimeModel] = {}
        self._lock = threading.RLock()

    # -- filesystem containment ------------------------------------------

    def _resolved_dirs(self) -> List[str]:
        resolved: List[str] = []
        for entry in self.model_dirs:
            try:
                candidate = os.path.realpath(os.path.abspath(
                    os.path.expanduser(str(entry))))
            except (OSError, ValueError):
                continue
            if os.path.isdir(candidate):
                resolved.append(candidate)
        return resolved

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
        for root in self._resolved_dirs():
            for path in self._scan(root):
                name = os.path.basename(path)
                model = self._describe(path, name)
                existing = loaded.get(model.model_id)
                if existing is not None:
                    model.loaded = True
                models[model.model_id] = model
        return [models[key] for key in sorted(models)]

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
                    "header_bytes", "declared_format",
                    "declared_architecture", "detail", "refused"):
            if key in described:
                metadata[key] = described[key]
        return RuntimeModel(
            model_id=RuntimeModel.make_id(self.name, name),
            name=name,
            backend=self.name,
            kind="unknown",
            context_window=0,
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

    def load_model(self, model: RuntimeModel,
                   token: Optional[CancellationToken] = None
                   ) -> RuntimeModel:
        """Admit an artifact into the resident set (metadata-verified)."""
        if token is not None:
            token.raise_if_cancelled()
        path = self.assert_loadable(model.path or model.name)
        with self._lock:
            resident = self._describe(path, os.path.basename(path))
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
            if token is not None and token.cancelled:
                return
            yield chunk

    # -- ops --------------------------------------------------------------

    def available(self) -> Tuple[bool, str]:
        """Discovery is always available; generation needs an adapter."""
        if self.adapter is None:
            return (True, "Artifact discovery only: " + _NATIVE_NO_ADAPTER)
        try:
            ok, detail = self.adapter.available()
        except Exception as exc:  # a broken adapter must not break the runtime
            return (False, "Inference adapter failed: {0}".format(
                _error_text(exc)))
        return (bool(ok), detail)

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
    "native": lambda config: NativeBackend(model_dirs=config.model_dirs),
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
        """Queue one item, checking cancellation while the queue is full."""
        while True:
            if self.token.cancelled:
                return False
            try:
                self._queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue

    def _produce(self) -> None:
        try:
            for chunk in self.backend.stream(self.request, self.token):
                if self.token.cancelled:
                    return
                if not self._emit(chunk):
                    return
            self._emit(_DONE)
        except BaseException as exc:  # noqa: BLE001 - isolate the backend
            self._emit(exc)

    def _iterate(self) -> Iterator[RuntimeChunk]:
        self._started_at = time.perf_counter()
        self.runtime._begin_stream(self)
        worker = threading.Thread(target=self._produce,
                                  name="forge-runtime-stream", daemon=True)
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
                try:
                    item = self._queue.get(
                        timeout=min(self.chunk_timeout, remaining))
                except queue.Empty:
                    self.token.cancel(CancelReason.TIMEOUT.value)
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

    def _finalize(self, failure: Optional[BaseException]) -> None:
        text = "".join(self._parts)
        latency = (time.perf_counter() - self._started_at) * 1000.0
        self._response.text = text
        self._response.latency_ms = latency
        self._response.backend = self.backend.name
        if not self._response.model:
            self._response.model = self.request.model
        self._response.metadata["chunks"] = self._chunks
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
            return RuntimeResponse.failure(
                "The model runtime is closed.", kind=ErrorKind.CLOSED.value,
                backend=request.backend, model=request.model,
                request_id=request.request_id, trace_id=request.trace_id)

        try:
            backend = self.select_backend(request.backend)
        except BackendNotFoundError as exc:
            return RuntimeResponse.failure(
                str(exc), kind=ErrorKind.NOT_FOUND.value,
                backend=request.backend, model=request.model,
                request_id=request.request_id, trace_id=request.trace_id)

        timeout = self.config.clamp_timeout(request.timeout)
        token = CancellationToken()
        with self._lock:
            if request.request_id in self._in_flight:
                return RuntimeResponse.failure(
                    "Request id {0!r} is already in flight.".format(
                        request.request_id),
                    kind=ErrorKind.CONFLICT.value, backend=backend.name,
                    model=request.model, request_id=request.request_id,
                    trace_id=request.trace_id)
            self._in_flight[request.request_id] = _InFlight(
                request_id=request.request_id, backend=backend.name,
                model=request.model, token=token)

        logger.debug("Runtime generate backend=%s model=%s request=%s "
                     "timeout=%.1fs prompt_chars=%d", backend.name,
                     request.model or "-", request.request_id, timeout,
                     len(request.prompt or ""))

        holder: Dict[str, Any] = {}

        def _work() -> None:
            try:
                holder["response"] = backend.generate(request, token)
            except BaseException as exc:  # noqa: BLE001 - isolate the backend
                holder["error"] = exc

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
            self._finish(request, response)
            return response

        if user_cancelled:
            partial = holder.get("response")
            text = partial.text if isinstance(partial, RuntimeResponse) else ""
            response = self._cancelled_response(
                request, backend.name, token.reason, started, text=text or "")
            self._finish(request, response)
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
            self._finish(request, response)
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
            self._finish(request, response)
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
        self._finish(request, result)
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

    def _finish(self, request: RuntimeRequest,
                response: RuntimeResponse) -> RuntimeResponse:
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

    # -- internals --------------------------------------------------------

    def _record_outcome(self, backend: str, response: RuntimeResponse,
                        *, streaming: bool = False) -> None:
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
            })
        logger.debug("Runtime outcome backend=%s success=%s kind=%s "
                     "latency=%.1fms", backend, response.success,
                     response.error_kind or "-", response.latency_ms)


def create_default_runtime(config: Optional[RuntimeConfig] = None,
                           *, load_config: bool = True) -> ModelRuntime:
    """Convenience constructor used by the CLI and the desktop app."""
    return ModelRuntime.from_defaults(config, load_config=load_config)
