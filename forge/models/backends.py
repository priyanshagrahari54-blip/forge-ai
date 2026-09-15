"""The stable Backend protocol for the inference fabric (Session 11).

One contract, eight operations::

    discover()  health()  load()  unload()
    generate()  stream()  cancel()  resource_requirements()

Backends are *explicit*: they are constructed and registered by the host
process, selected by id per request, and never imported from a
data-supplied dotted path. An optional provider that is not installed is
reported as unavailable — it is never pretended to exist.

Canonical implementation
------------------------
:class:`RuntimeBackendAdapter` delegates every operation to a
:class:`~forge.runtime.model_runtime.ModelRuntime` and a backend name, so the
Native Model Runtime stays the single execution authority::

    Fabric Backend -> ModelRuntime -> ModelBackend -> model

Nothing in this module can be used to bypass the runtime, the resource
governor, or A33: it adds an explicit, status-reporting seam on top of them.

Honest status reporting
-----------------------
:class:`BackendStatus` keeps four questions separate, because conflating them
is how a system ends up claiming a provider works:

``configured``  an endpoint/artifact/driver is present in configuration.
``reachable``   a live probe actually answered.
``verified``    a real check proved the backend serves the model it claims.
``ready``       configured AND reachable AND verified AND not denied.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterator, List, Optional, Tuple)

from forge.models.identity import (AvailabilityState, ModelIdentity,
                                   VerificationState)

__all__ = [
    "Backend",
    "BackendError",
    "BackendNotReadyError",
    "BackendRegistry",
    "BackendStatus",
    "ForgeCustomBackend",
    "LlamaCppCompatibleBackend",
    "NativeLocalBackend",
    "OllamaCompatibleBackend",
    "RemoteProviderBackend",
    "ResourceRequirements",
    "RuntimeBackendAdapter",
]


class BackendError(RuntimeError):
    """A backend refused or failed an operation."""

    code = "BACKEND"

    def __init__(self, message: str, *, code: str = "BACKEND") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class BackendNotReadyError(BackendError):
    """The backend is not ready (unconfigured, unreachable, or unverified)."""

    code = "BACKEND_NOT_READY"


@dataclass(frozen=True)
class ResourceRequirements:
    """What one backend/model needs. Zero/None means *not reported*."""

    model_memory_bytes: int = 0
    min_available_mb: int = 0
    concurrency_slots: int = 1
    cpu_threads: int = 0
    requires_network: bool = False
    requires_local_loading: bool = False
    device_profiles: Tuple[str, ...] = ()
    measured: bool = False
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_memory_bytes": int(self.model_memory_bytes or 0),
            "min_available_mb": int(self.min_available_mb or 0),
            "concurrency_slots": int(self.concurrency_slots or 1),
            "cpu_threads": int(self.cpu_threads or 0),
            "requires_network": bool(self.requires_network),
            "requires_local_loading": bool(self.requires_local_loading),
            "device_profiles": list(self.device_profiles),
            "measured": bool(self.measured),
            "detail": self.detail,
        }


@dataclass
class BackendStatus:
    """Separate answers to configured / reachable / verified / ready."""

    backend_id: str
    kind: str = "custom"
    configured: bool = False
    reachable: bool = False
    verified: bool = False
    ready: bool = False
    local: bool = True
    requires_network: bool = False
    detail: str = ""
    error: str = ""
    latency_ms: float = 0.0
    checked_at: float = 0.0
    models_available: int = 0
    models_loaded: int = 0
    #: Set when a governor/policy refused this backend outright.
    denial: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def recompute(self) -> "BackendStatus":
        """``ready`` is derived, never asserted: all three must hold."""
        self.ready = bool(self.configured and self.reachable
                          and self.verified and not self.denial)
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "kind": self.kind,
            "configured": bool(self.configured),
            "reachable": bool(self.reachable),
            "verified": bool(self.verified),
            "ready": bool(self.ready),
            "local": bool(self.local),
            "requires_network": bool(self.requires_network),
            "detail": self.detail,
            "error": self.error,
            "denial": self.denial,
            "latency_ms": round(float(self.latency_ms or 0.0), 3),
            "checked_at": self.checked_at,
            "models_available": int(self.models_available or 0),
            "models_loaded": int(self.models_loaded or 0),
            "extra": dict(self.extra),
        }


class Backend:
    """The one stable backend contract.

    Subclasses implement the eight operations. The defaults are honest: an
    unimplemented operation reports itself unavailable rather than faking a
    result, and ``generate``/``stream`` raise :class:`BackendNotReadyError`.
    """

    #: Stable id, unique inside one registry. Must not contain ":".
    backend_id: str = ""
    #: ``native`` | ``ollama`` | ``llama_cpp`` | ``forge`` | ``remote`` |
    #: ``custom``.
    kind: str = "custom"
    description: str = ""
    local: bool = True
    requires_network: bool = False
    #: Free-first posture: a paid backend is never selected silently.
    free: bool = True

    def __init__(self, backend_id: str = "") -> None:
        if backend_id:
            self.backend_id = backend_id
        self._lock = threading.RLock()
        self._in_flight: Dict[str, Dict[str, Any]] = {}

    # -- contract --------------------------------------------------------

    def validate(self) -> None:
        if not self.backend_id or not str(self.backend_id).strip():
            raise BackendError("a backend_id is required")
        if ":" in self.backend_id:
            raise BackendError(
                "backend_id %r must not contain ':' (it separates backend "
                "from model in model ids)" % self.backend_id)
        for name in ("discover", "health", "load", "unload", "generate",
                     "stream", "cancel", "resource_requirements"):
            if not callable(getattr(self, name, None)):
                raise BackendError(
                    "backend %r does not implement %s()"
                    % (self.backend_id, name))

    def discover(self) -> List[ModelIdentity]:
        """Return the identities this backend can actually see."""
        return []

    def health(self, probe: bool = True) -> BackendStatus:
        """Backend health. ``probe=False`` never touches a network/artifact."""
        status = BackendStatus(backend_id=self.backend_id, kind=self.kind,
                               local=self.local,
                               requires_network=self.requires_network,
                               configured=False, reachable=False,
                               verified=False,
                               detail="not implemented",
                               checked_at=time.time())
        return status.recompute()

    def load(self, model_id: str, *,
             token: Any = None) -> ModelIdentity:
        raise BackendNotReadyError(
            "backend %r does not support model loading" % self.backend_id)

    def unload(self, model_id: str) -> bool:
        return False

    def generate(self, request: Any, *, token: Any = None) -> Any:
        raise BackendNotReadyError(
            "backend %r cannot run inference" % self.backend_id)

    def stream(self, request: Any, *, token: Any = None) -> Iterator[Any]:
        raise BackendNotReadyError(
            "backend %r cannot stream inference" % self.backend_id)
        yield  # pragma: no cover - makes this a generator

    def cancel(self, request_id: str, *, reason: str = "cancelled") -> bool:
        """Cancel one in-flight request. ``True`` when a token was signalled."""
        with self._lock:
            entry = self._in_flight.get(request_id)
            if entry is None:
                return False
            token = entry.get("token")
        if token is None:
            return False
        try:
            return bool(token.cancel(reason))
        except Exception:
            return False

    def resource_requirements(self, model_id: str = ""
                              ) -> ResourceRequirements:
        return ResourceRequirements(requires_network=self.requires_network,
                                    requires_local_loading=self.local,
                                    detail="no report from backend %r"
                                           % self.backend_id)

    # -- in-flight bookkeeping (shared by adapters) -----------------------

    def _track(self, request_id: str, token: Any, model_id: str) -> None:
        with self._lock:
            if len(self._in_flight) >= 512:
                # Bounded: drop the oldest entry rather than growing forever.
                oldest = sorted(self._in_flight.items(),
                                key=lambda kv: kv[1].get("at", 0.0))[:64]
                for key, _ in oldest:
                    self._in_flight.pop(key, None)
            self._in_flight[request_id] = {"token": token, "model": model_id,
                                           "at": time.time()}

    def _untrack(self, request_id: str) -> None:
        with self._lock:
            self._in_flight.pop(request_id, None)

    def in_flight(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{"request_id": key, "model_id": value.get("model", ""),
                     "started_at": value.get("at", 0.0)}
                    for key, value in sorted(self._in_flight.items())]


class RuntimeBackendAdapter(Backend):
    """Canonical backend: every operation goes through the ModelRuntime.

    The adapter holds no inference logic of its own. It translates between the
    fabric's :class:`~forge.models.identity.ModelIdentity` vocabulary and the
    runtime's ``RuntimeModel``/``RuntimeRequest``/``RuntimeResponse`` types,
    and it refuses to run when the runtime says the backend is unavailable.
    """

    def __init__(self, runtime: Any, backend_name: str, *,
                 backend_id: str = "", kind: str = "custom",
                 local: bool = True, requires_network: bool = False,
                 free: bool = True, description: str = "",
                 capabilities: Tuple[str, ...] = (),
                 network_guard: Optional[Callable[[str], str]] = None,
                 governor: Any = None) -> None:
        super().__init__(backend_id or backend_name)
        if runtime is None:
            raise BackendError("a ModelRuntime instance is required")
        self.runtime = runtime
        self.backend_name = backend_name or backend_id
        self.kind = kind
        self.local = bool(local)
        self.requires_network = bool(requires_network)
        self.free = bool(free)
        self.description = description or ("Forge Native Model Runtime "
                                           "backend %r" % self.backend_name)
        self.capabilities = tuple(capabilities or ())
        #: Optional policy hook: returns "" when the endpoint is allowed, or a
        #: refusal reason. Used to keep an Ollama endpoint localhost-only and
        #: a remote endpoint inside the A33 network policy.
        self._network_guard = network_guard
        self._governor = governor
        self._verified_models: Dict[str, float] = {}
        self.validate()

    # -- guards ----------------------------------------------------------

    def _guard_reason(self) -> str:
        if self._network_guard is None:
            return ""
        try:
            return str(self._network_guard(self.backend_name) or "")
        except Exception as exc:  # a broken guard fails closed
            return "network guard error: %s" % (exc,)

    def _runtime_backend(self) -> Any:
        return self.runtime.get_backend(self.backend_name)

    def _info(self) -> Any:
        try:
            return self._runtime_backend().info()
        except Exception:
            return None

    # -- contract --------------------------------------------------------

    def discover(self) -> List[ModelIdentity]:
        """Discover identities through the runtime (never invents a model)."""
        reason = self._guard_reason()
        if reason:
            raise BackendNotReadyError(
                "backend %r refused discovery: %s" % (self.backend_id, reason),
                code="NETWORK_POLICY")
        try:
            self.runtime.discover(self.backend_name)
            found = self.runtime.models(self.backend_name)
        except Exception as exc:
            raise BackendNotReadyError(
                "discovery failed on backend %r: %s"
                % (self.backend_id, _text(exc))) from exc
        identities: List[ModelIdentity] = []
        for entry in found:
            #: Cost posture comes from the backend, not from a hopeful default:
            #: a paid remote provider must never be labelled free, or the
            #: free-first policy would silently spend money.
            identity = ModelIdentity.from_runtime_model(
                entry, provider_id=self.backend_id,
                capabilities=self.capabilities, free=bool(self.free),
                cost_per_token=float(getattr(self, "cost_per_token", 0.0)
                                     or 0.0))
            if identity.model_id in self._verified_models:
                identity.set_verification(VerificationState.VERIFIED.value,
                                          method="cached")
                identity.set_availability(AvailabilityState.READY.value,
                                          reason="previously verified")
            identities.append(identity)
        return identities

    def health(self, probe: bool = True) -> BackendStatus:
        denial = self._guard_reason()
        status = BackendStatus(backend_id=self.backend_id, kind=self.kind,
                               local=self.local,
                               requires_network=self.requires_network,
                               denial=denial, checked_at=time.time())
        info = self._info()
        if info is None:
            status.detail = ("backend %r is not registered with the runtime"
                             % self.backend_name)
            status.error = status.detail
            return status.recompute()
        status.configured = True
        status.detail = str(getattr(info, "detail", "") or "")
        if denial:
            status.error = denial
            return status.recompute()
        if not bool(getattr(info, "available", False)):
            status.detail = status.detail or "backend reports unavailable"
            return status.recompute()
        try:
            items = self.runtime.health(self.backend_name, probe=probe)
        except Exception as exc:
            status.error = _text(exc)
            return status.recompute()
        for item in items:
            if getattr(item, "backend", "") != self.backend_name:
                continue
            payload = item.to_dict()
            status.latency_ms = float(payload.get("latency_ms") or 0.0)
            status.models_available = int(payload.get("models_available") or 0)
            status.models_loaded = int(payload.get("models_loaded") or 0)
            status.detail = str(payload.get("detail") or status.detail)
            status.error = str(payload.get("error") or "")
            state = str(payload.get("status") or "")
            status.reachable = bool(probe and state == "ready")
            if not probe:
                # An unprobed health check cannot claim reachability.
                status.reachable = False
                status.detail = (status.detail + " (not probed)").strip()
        status.verified = any(
            model_id in self._verified_models
            for model_id in self._verified_models)
        return status.recompute()

    def load(self, model_id: str, *, token: Any = None) -> ModelIdentity:
        """Load through the runtime (which enforces the governor)."""
        reason = self._guard_reason()
        if reason:
            raise BackendNotReadyError(
                "backend %r refused to load %r: %s"
                % (self.backend_id, model_id, reason), code="NETWORK_POLICY")
        try:
            loaded = self.runtime.load(model_id, self.backend_name)
        except Exception as exc:
            code = "RESOURCE_DENIED" if _is_capacity(exc) else "LOAD_FAILED"
            raise BackendError(
                "load of %r on backend %r failed: %s"
                % (model_id, self.backend_id, _text(exc)), code=code) from exc
        identity = ModelIdentity.from_runtime_model(
            loaded, provider_id=self.backend_id,
            capabilities=self.capabilities or tuple(
                getattr(loaded, "capabilities", ()) or ()))
        identity.set_availability(AvailabilityState.LOADED.value,
                                  reason="runtime load")
        return identity

    def unload(self, model_id: str) -> bool:
        try:
            return bool(self.runtime.unload(model_id, self.backend_name))
        except Exception:
            return False

    def generate(self, request: Any, *, token: Any = None) -> Any:
        reason = self._guard_reason()
        if reason:
            raise BackendNotReadyError(
                "backend %r refused generation: %s"
                % (self.backend_id, reason), code="NETWORK_POLICY")
        request = self._address(request)
        request_id = str(getattr(request, "request_id", "") or "")
        self._track(request_id, token, str(getattr(request, "model", "")))
        try:
            if token is not None:
                # The runtime owns the bounded worker + timeout; the caller's
                # token is honoured by cancelling the runtime request.
                watcher = _CancelWatcher(self.runtime, request_id, token)
                watcher.start()
                try:
                    return self.runtime.generate(request)
                finally:
                    watcher.stop()
            return self.runtime.generate(request)
        finally:
            self._untrack(request_id)

    def stream(self, request: Any, *, token: Any = None) -> Iterator[Any]:
        reason = self._guard_reason()
        if reason:
            raise BackendNotReadyError(
                "backend %r refused streaming: %s"
                % (self.backend_id, reason), code="NETWORK_POLICY")
        request = self._address(request)
        request_id = str(getattr(request, "request_id", "") or "")
        self._track(request_id, token, str(getattr(request, "model", "")))
        watcher = None
        try:
            handle = self.runtime.stream(request)
            if token is not None:
                watcher = _CancelWatcher(self.runtime, request_id, token,
                                         stream=handle)
                watcher.start()
            for chunk in handle:
                yield chunk
        finally:
            if watcher is not None:
                watcher.stop()
            self._untrack(request_id)

    def resource_requirements(self, model_id: str = ""
                              ) -> ResourceRequirements:
        size = 0
        context = 0
        try:
            if model_id:
                model = self.runtime.resolve_model(model_id, self.backend_name)
                size = int(getattr(model, "size_bytes", 0) or 0)
                context = int(getattr(model, "context_window", 0) or 0)
        except Exception:
            size = 0
        try:
            resources = self.runtime.resources().to_dict()
        except Exception:
            resources = {}
        return ResourceRequirements(
            model_memory_bytes=size,
            min_available_mb=int((size // (1024 * 1024)) * 1.2) if size else 0,
            concurrency_slots=1,
            cpu_threads=int(resources.get("cpu_count") or 0),
            requires_network=self.requires_network,
            requires_local_loading=self.local,
            measured=bool(size),
            detail=("model_id=%s size_bytes=%d context_window=%d"
                    % (model_id or "-", size, context)))

    # -- verification bookkeeping ----------------------------------------

    def mark_verified(self, model_id: str, *, ttl_seconds: float = 0.0) -> None:
        with self._lock:
            self._verified_models[model_id] = time.time() + max(
                0.0, float(ttl_seconds or 0.0))

    def mark_unverified(self, model_id: str) -> None:
        with self._lock:
            self._verified_models.pop(model_id, None)

    def verified_models(self) -> List[str]:
        now = time.time()
        with self._lock:
            expired = [key for key, until in self._verified_models.items()
                       if until and until < now]
            for key in expired:
                self._verified_models.pop(key, None)
            return sorted(self._verified_models)

    # -- helpers ---------------------------------------------------------

    def _address(self, request: Any) -> Any:
        """Pin the request to this backend so it cannot be silently retargeted."""
        current = str(getattr(request, "backend", "") or "")
        if current and current != self.backend_name:
            raise BackendError(
                "request addresses backend %r but was handed to %r"
                % (current, self.backend_name), code="BACKEND_MISMATCH")
        try:
            request.backend = self.backend_name
        except Exception:
            pass
        return request


class _CancelWatcher:
    """Propagates a caller's cancellation token to a runtime request.

    The runtime already bounds every call with a timeout and its own
    cancellation; this only bridges an *external* fence/cancel (for example a
    scheduler attempt that was fenced while inference was running).
    """

    def __init__(self, runtime: Any, request_id: str, token: Any,
                 stream: Any = None, interval: float = 0.02) -> None:
        self.runtime = runtime
        self.request_id = request_id
        self.token = token
        self.stream = stream
        self.interval = max(0.005, float(interval))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.token is None:
            return
        self._thread = threading.Thread(target=self._run,
                                        name="forge-backend-cancel",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                cancelled = bool(self.token.cancelled)
                reason = str(getattr(self.token, "reason", "") or "cancelled")
            except Exception:
                return
            if cancelled:
                try:
                    self.runtime.cancel(self.request_id, reason=reason)
                except Exception:
                    pass
                if self.stream is not None:
                    try:
                        self.stream.cancel(reason)
                    except Exception:
                        pass
                return
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(0.1)


class NativeLocalBackend(RuntimeBackendAdapter):
    """The first-party local backend (runtime backend ``native``).

    It discovers and describes real model artifacts and, when an
    ``InferenceAdapter`` is configured, runs real local inference. Without an
    adapter it reports unavailable rather than fabricating output.
    """

    def __init__(self, runtime: Any, *, backend_id: str = "native",
                 capabilities: Tuple[str, ...] = (),
                 governor: Any = None) -> None:
        super().__init__(runtime, "native", backend_id=backend_id,
                         kind="native", local=True, requires_network=False,
                         free=True, capabilities=capabilities,
                         governor=governor,
                         description=("First-party local backend: discovers "
                                      "real artifacts and runs inference "
                                      "through a configured adapter."))

    def health(self, probe: bool = True) -> BackendStatus:
        status = super().health(probe=probe)
        if status.configured and not status.reachable and probe:
            detail = (status.detail or "").lower()
            if "adapter" in detail or "inference" in detail:
                status.error = status.error or (
                    "no inference adapter is configured; the native backend "
                    "will not fabricate output")
        return status


class OllamaCompatibleBackend(RuntimeBackendAdapter):
    """Optional Ollama-compatible backend (runtime backend ``ollama``).

    Localhost-aware by construction: unless ``allow_non_loopback`` is set, a
    non-loopback endpoint is refused by the network guard, so an Ollama URL
    can never be used to reach an arbitrary host. Network access itself stays
    off until the runtime configuration enables it.
    """

    def __init__(self, runtime: Any, *, backend_id: str = "ollama",
                 base_url: str = "http://127.0.0.1:11434",
                 allow_non_loopback: bool = False,
                 capabilities: Tuple[str, ...] = (),
                 governor: Any = None) -> None:
        self.base_url = str(base_url or "")
        self.allow_non_loopback = bool(allow_non_loopback)
        super().__init__(runtime, "ollama", backend_id=backend_id,
                         kind="ollama", local=True, requires_network=True,
                         free=True, capabilities=capabilities,
                         governor=governor,
                         network_guard=self._guard,
                         description=("Ollama-compatible local server at %s "
                                      "(explicit, health-checked, "
                                      "network-policy controlled)."
                                      % self.base_url))

    def _guard(self, _backend_name: str) -> str:
        """Refuse a non-loopback endpoint unless explicitly allowed."""
        from urllib.parse import urlsplit

        url = self.base_url or ""
        if not url:
            return "no Ollama endpoint is configured"
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return "unparsable Ollama endpoint"
        loopback = host in ("localhost", "127.0.0.1", "::1", "[::1]")
        if not loopback and not self.allow_non_loopback:
            return ("Ollama endpoint host %r is not loopback; set "
                    "allow_non_loopback explicitly to permit it" % host)
        return ""


class LlamaCppCompatibleBackend(RuntimeBackendAdapter):
    """Optional llama.cpp-compatible backend (runtime backend ``llama_cpp``).

    The runtime never imports ``llama_cpp``: a client instance is injected by
    the host process. Without one, the backend reports unavailable.
    """

    def __init__(self, runtime: Any, *, backend_id: str = "llama_cpp",
                 capabilities: Tuple[str, ...] = (),
                 governor: Any = None) -> None:
        super().__init__(runtime, "llama_cpp", backend_id=backend_id,
                         kind="llama_cpp", local=True,
                         requires_network=False, free=True,
                         capabilities=capabilities, governor=governor,
                         description=("llama.cpp-compatible backend; requires "
                                      "an explicitly injected client."))


class ForgeCustomBackend(RuntimeBackendAdapter):
    """Forge's own custom backend seam (runtime backend ``forge``)."""

    def __init__(self, runtime: Any, *, backend_id: str = "forge",
                 capabilities: Tuple[str, ...] = (),
                 governor: Any = None) -> None:
        super().__init__(runtime, "forge", backend_id=backend_id,
                         kind="forge", local=True, requires_network=False,
                         free=True, capabilities=capabilities,
                         governor=governor,
                         description=("Forge custom backend; requires an "
                                      "explicitly injected client."))


class RemoteProviderBackend(RuntimeBackendAdapter):
    """Remote provider adapter (an ``RemoteHttpBackend`` inside the runtime).

    Remote inference is always optional, always explicit, and never free-first:
    ``free=False`` keeps the router from selecting a paid provider unless the
    policy allows paid and remote.
    """

    def __init__(self, runtime: Any, backend_name: str, *,
                 backend_id: str = "", provider_id: str = "",
                 capabilities: Tuple[str, ...] = (),
                 network_guard: Optional[Callable[[str], str]] = None,
                 governor: Any = None, description: str = "",
                 cost_per_token: float = 0.0) -> None:
        super().__init__(runtime, backend_name,
                         backend_id=backend_id or backend_name,
                         kind="remote", local=False, requires_network=True,
                         free=False, capabilities=capabilities,
                         governor=governor, network_guard=network_guard,
                         description=description or (
                             "Remote provider adapter %r" % provider_id))
        self.provider_id = provider_id or backend_name
        #: Declared cost per token (0 = "the provider did not say"). Never
        #: guessed: an unknown cost is not the same as a free model.
        self.cost_per_token = float(cost_per_token or 0.0)


class BackendRegistry:
    """Explicit, closed registry of backends by id."""

    def __init__(self, backends: Optional[List[Backend]] = None) -> None:
        self._backends: Dict[str, Backend] = {}
        self._lock = threading.RLock()
        self._statuses: Dict[str, BackendStatus] = {}
        for backend in backends or ():
            self.register(backend)

    def register(self, backend: Backend, *, replace: bool = False) -> Backend:
        if backend is None:
            raise BackendError("a Backend instance is required")
        backend.validate()
        with self._lock:
            if backend.backend_id in self._backends and not replace:
                raise BackendError(
                    "backend already registered: %r" % backend.backend_id)
            self._backends[backend.backend_id] = backend
            self._statuses.pop(backend.backend_id, None)
        return backend

    def unregister(self, backend_id: str) -> bool:
        with self._lock:
            self._statuses.pop(backend_id, None)
            return self._backends.pop(backend_id, None) is not None

    def get(self, backend_id: str) -> Backend:
        with self._lock:
            try:
                return self._backends[backend_id]
            except KeyError:
                raise BackendError(
                    "unknown backend %r (registered: %s)"
                    % (backend_id, ", ".join(sorted(self._backends)) or "-"),
                    code="BACKEND_NOT_FOUND") from None

    def has(self, backend_id: str) -> bool:
        with self._lock:
            return backend_id in self._backends

    def ids(self) -> List[str]:
        with self._lock:
            return sorted(self._backends)

    def list(self) -> List[Backend]:
        with self._lock:
            return [self._backends[key] for key in sorted(self._backends)]

    def statuses(self, *, probe: bool = True,
                 refresh: bool = True) -> List[BackendStatus]:
        """Health of every backend, with the four answers kept separate."""
        out: List[BackendStatus] = []
        with self._lock:
            backends = list(self._backends.items())
        for backend_id, backend in backends:
            if not refresh and backend_id in self._statuses:
                out.append(self._statuses[backend_id])
                continue
            try:
                status = backend.health(probe=probe)
            except Exception as exc:
                status = BackendStatus(
                    backend_id=backend_id, kind=getattr(backend, "kind", ""),
                    configured=False, reachable=False, verified=False,
                    error=_text(exc), checked_at=time.time()).recompute()
            with self._lock:
                self._statuses[backend_id] = status
            out.append(status)
        return out

    def ready_ids(self, *, probe: bool = True) -> List[str]:
        return [status.backend_id
                for status in self.statuses(probe=probe) if status.ready]

    def snapshot(self, *, probe: bool = False) -> Dict[str, Any]:
        return {"backends": [status.to_dict()
                             for status in self.statuses(probe=probe)]}

    def __len__(self) -> int:
        with self._lock:
            return len(self._backends)

    def __contains__(self, backend_id: object) -> bool:
        return isinstance(backend_id, str) and self.has(backend_id)


def _text(exc: BaseException) -> str:
    """Bounded, redacted error text."""
    try:
        from forge.runtime.model_runtime import redact_text
        return redact_text(str(exc))[:500]
    except Exception:
        return str(exc)[:500]


def _is_capacity(exc: BaseException) -> bool:
    name = type(exc).__name__
    return name in ("RuntimeCapacityError", "ResourceLimitExceeded") \
        or "RESOURCE" in str(getattr(exc, "code", "")).upper()
