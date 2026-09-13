"""Typed server inference service (Session 11).

The Forge Server is the authoritative place for heavy inference: a G560 thin
client submits typed requests and displays results, and the server owns policy,
resource limits, model selection, execution and audit.

Operations (closed set — there is no arbitrary command endpoint)::

    models.list      models.status    models.verify
    models.load      models.unload
    inference.generate  inference.stream  inference.stream_events
    inference.cancel    inference.status

Every operation is schema-validated at the API layer, authorized against the
closed scope table, bounded (prompt/output/timeout/concurrency), audited, and
redacted. A refusal is a structured error, never a synthesized answer.

Streaming is cursor-based, matching the server's existing pull-based event
design: ``inference.stream`` starts a bounded producer and returns the first
events; ``inference.stream_events`` long-polls from a cursor, so a thin client
can disconnect and resume without losing or duplicating a chunk. Buffers are
bounded and completed streams expire.

Python floor: 3.8 (Windows 7 reference target). Stdlib + the server's own
dependencies only.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import (Any, Callable, Deque, Dict, Iterator, List, Optional,
                    Tuple)

from forge.server.errors import (InvalidRequest, NotFound, PermissionDenied,
                                 ServerError)

__all__ = [
    "InferenceNotConfigured",
    "InferenceServiceConfig",
    "ServerInferenceService",
    "SERVER_INFERENCE_OPERATIONS",
]

#: The closed operation set this service implements.
SERVER_INFERENCE_OPERATIONS: Tuple[str, ...] = (
    "models.list", "models.status", "models.verify", "models.load",
    "models.unload", "inference.generate", "inference.stream",
    "inference.stream_events", "inference.cancel", "inference.status",
)

#: Bounds. Nothing here is infinite, and none of it is caller-controlled
#: beyond the documented maximums.
MAX_PROMPT_CHARS = 32 * 1024
MAX_CONTEXT_CHARS = 128 * 1024
MAX_OUTPUT_TOKENS = 4096
MAX_TIMEOUT_SECONDS = 300.0
MAX_STREAM_CHARS = 128 * 1024
#: Completed streams are retained this long for cursor replay.
STREAM_TTL_SECONDS = 300.0
MAX_RETAINED_STREAMS = 64
MAX_EVENTS_PER_STREAM = 2048

#: Fields a finished stream reports back to a thin client. Never ``text``
#: (the client already has the deltas) and never prompt/context content.
_STREAM_SUMMARY_KEYS = frozenset({
    "request_id", "task_id", "attempt_id", "generation_id", "model_id",
    "backend_id", "provider", "success", "state", "neural", "done",
    "streamed", "truncated", "error", "error_code", "finish_reason",
    "latency_ms", "time_to_first_token_ms", "input_tokens", "output_tokens",
    "verification_state", "availability_state", "output_scan"})


class InferenceNotConfigured(ServerError):
    """Server inference is disabled (the default) or has no usable backend."""

    code = "INFERENCE_NOT_CONFIGURED"
    status = 503


@dataclass
class InferenceServiceConfig:
    """Explicit configuration. Disabled by default: nothing is loaded,
    discovered or contacted until an operator turns it on."""

    enabled: bool = False
    #: Directories holding local artifacts the native/reference backends may
    #: read. Empty means "no local model discovery".
    reference_dirs: Tuple[str, ...] = ()
    model_dirs: Tuple[str, ...] = ()
    #: Network access stays off unless explicitly enabled.
    allow_network: bool = False
    #: Provider ids resolved from ``FORGE_REMOTE_<ID>_*`` environment vars.
    remote_providers: Tuple[str, ...] = ()
    #: Ollama endpoint (loopback only unless ``allow_remote_ollama``).
    ollama_url: str = ""
    allow_remote_ollama: bool = False
    max_resident_bytes: int = 2 * 1024 * 1024 * 1024
    max_model_slots: int = 2
    idle_unload_seconds: float = 0.0
    auto_verify: bool = False
    require_verified: bool = True
    default_timeout_seconds: float = 60.0
    resource_profile: str = ""
    #: Cap on concurrently running server-side generations.
    max_concurrent_requests: int = 2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "reference_dirs": list(self.reference_dirs),
            "model_dirs": list(self.model_dirs),
            "allow_network": bool(self.allow_network),
            "remote_providers": list(self.remote_providers),
            "ollama_url": self.ollama_url,
            "allow_remote_ollama": bool(self.allow_remote_ollama),
            "max_resident_bytes": int(self.max_resident_bytes),
            "max_model_slots": int(self.max_model_slots),
            "idle_unload_seconds": float(self.idle_unload_seconds),
            "auto_verify": bool(self.auto_verify),
            "require_verified": bool(self.require_verified),
            "default_timeout_seconds": float(self.default_timeout_seconds),
            "resource_profile": self.resource_profile,
            "max_concurrent_requests": int(self.max_concurrent_requests),
        }

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "InferenceServiceConfig":
        """Build a config from ``FORGE_SERVER_INFERENCE_*`` variables."""
        import os

        source = dict(os.environ if env is None else env)

        def flag(name: str) -> bool:
            return str(source.get(name, "")).strip().lower() in ("1", "true",
                                                                 "yes", "on")

        def paths(name: str) -> Tuple[str, ...]:
            raw = str(source.get(name, "") or "")
            return tuple(item.strip() for item in
                         raw.replace(";", ",").split(",") if item.strip())

        def number(name: str, default: float) -> float:
            try:
                return float(source.get(name, "") or default)
            except ValueError:
                return float(default)

        return cls(
            enabled=flag("FORGE_SERVER_INFERENCE"),
            reference_dirs=paths("FORGE_SERVER_INFERENCE_REFERENCE_DIRS"),
            model_dirs=paths("FORGE_SERVER_INFERENCE_MODEL_DIRS"),
            allow_network=flag("FORGE_SERVER_INFERENCE_ALLOW_NETWORK"),
            remote_providers=paths("FORGE_SERVER_INFERENCE_REMOTE_PROVIDERS"),
            ollama_url=str(source.get("FORGE_SERVER_INFERENCE_OLLAMA_URL", "")
                           or ""),
            allow_remote_ollama=flag(
                "FORGE_SERVER_INFERENCE_ALLOW_REMOTE_OLLAMA"),
            max_resident_bytes=int(number(
                "FORGE_SERVER_INFERENCE_MAX_RESIDENT_BYTES",
                2 * 1024 * 1024 * 1024)),
            max_model_slots=int(number(
                "FORGE_SERVER_INFERENCE_MAX_MODEL_SLOTS", 2)),
            idle_unload_seconds=number(
                "FORGE_SERVER_INFERENCE_IDLE_UNLOAD_SECONDS", 0.0),
            auto_verify=flag("FORGE_SERVER_INFERENCE_AUTO_VERIFY"),
            require_verified=not flag(
                "FORGE_SERVER_INFERENCE_ALLOW_UNVERIFIED"),
            default_timeout_seconds=min(
                MAX_TIMEOUT_SECONDS,
                number("FORGE_SERVER_INFERENCE_TIMEOUT", 60.0)),
            resource_profile=str(
                source.get("FORGE_RESOURCE_PROFILE", "") or ""),
            max_concurrent_requests=int(number(
                "FORGE_SERVER_INFERENCE_MAX_CONCURRENT", 2)),
        )


@dataclass
class _RetainedStream:
    """Server-side state for one bounded stream (cursor replay)."""

    stream_id: str
    request_id: str
    model_id: str = ""
    backend_id: str = ""
    events: Deque[Any] = field(default_factory=lambda: deque(
        maxlen=MAX_EVENTS_PER_STREAM))
    done: bool = False
    state: str = ""
    error: str = ""
    error_code: str = ""
    #: Text-free summary of the finished generation (provenance for a thin
    #: client: which model answered, whether it was neural, why it ended).
    summary: Dict[str, Any] = field(default_factory=dict)
    #: Final stream snapshot (sequence, chars, complete, truncation).
    stream_snapshot: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    dropped: int = 0
    notify: threading.Condition = field(
        default_factory=lambda: threading.Condition())

    def append(self, event: Any) -> None:
        with self.notify:
            if len(self.events) >= MAX_EVENTS_PER_STREAM:
                self.dropped += 1
            self.events.append(event)
            self.notify.notify_all()

    def finish(self, *, state: str, error: str = "",
               error_code: str = "") -> None:
        with self.notify:
            self.done = True
            self.state = state
            self.error = error[:400]
            self.error_code = error_code
            self.finished_at = time.time()
            self.notify.notify_all()

    def since(self, after: int) -> Tuple[List[Dict[str, Any]], int, bool]:
        with self.notify:
            selected = [event.to_wire() for event in self.events
                        if int(getattr(event, "sequence", 0)) > after]
            last = int(getattr(self.events[-1], "sequence", 0)
                       ) if self.events else after
            return selected, last, self.done


class ServerInferenceService:
    """The server's authoritative inference surface."""

    def __init__(self, *, config: Optional[InferenceServiceConfig] = None,
                 governor: Any = None, audit: Any = None,
                 emit: Optional[Callable[..., None]] = None,
                 fabric: Any = None, fences: Any = None) -> None:
        self.config = config or InferenceServiceConfig()
        self.governor = governor
        self.audit = audit
        self._emit = emit
        #: An explicitly supplied InferenceFabric (tests, embeddings).
        self._fabric = fabric
        #: Optional attempt-fence authority (a :class:`FenceRegistry`, e.g. the
        #: one a DAG scheduler owns). When present, a generation started for a
        #: task is bound to that task's current fence, so an attempt that is
        #: superseded or cancelled mid-flight cannot publish its result. The
        #: server's own lease fencing is separate and unchanged.
        self.fences = fences
        self._lock = threading.RLock()
        self._streams: Dict[str, _RetainedStream] = {}
        self._running = 0
        self._semaphore = threading.BoundedSemaphore(
            max(1, int(self.config.max_concurrent_requests)))
        self._started = False
        self._start_error = ""
        self._counts: Dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def fabric(self) -> Any:
        """The inference fabric, built on first use (never at import time)."""
        with self._lock:
            if self._fabric is not None:
                return self._fabric
            if not self.config.enabled:
                raise InferenceNotConfigured(
                    "server inference is disabled; set "
                    "FORGE_SERVER_INFERENCE=1 (or pass an explicit "
                    "InferenceServiceConfig) to enable it")
            if self._start_error:
                raise InferenceNotConfigured(self._start_error)
            try:
                self._fabric = self._build()
                self._started = True
            except Exception as exc:
                self._start_error = "could not start inference: %s" % (
                    _redact(str(exc)),)
                raise InferenceNotConfigured(self._start_error) from exc
            return self._fabric

    def _build(self) -> Any:
        from forge.core.resource_governor import (ResourceGovernor,
                                                  select_profile)
        from forge.models.engine import build_inference_fabric
        from forge.models.remote import RemoteProviderConfig
        from forge.runtime.model_runtime import RuntimeConfig

        governor = self.governor
        if governor is None:
            governor = ResourceGovernor(
                select_profile(self.config.resource_profile))
        else:
            governor = _tighten_governor(governor,
                                         self.config.resource_profile,
                                         count=self._count)
        #: The effective governor for inference (may be a stricter, dedicated
        #: one; never a looser one than the server's).
        self.governor = governor

        config = RuntimeConfig.load()
        if self.config.allow_network and not config.allow_network:
            config.allow_network = True
        if self.config.model_dirs:
            existing = list(config.model_dirs)
            for item in self.config.model_dirs:
                if item not in existing:
                    existing.append(item)
            config.model_dirs = tuple(existing)
        if self.config.ollama_url and "ollama" not in config.backends:
            config.backends = tuple(list(config.backends) + ["ollama"])
            config.ollama_url = self.config.ollama_url
        config.validate()

        remotes: List[RemoteProviderConfig] = []
        for provider_id in self.config.remote_providers:
            try:
                remotes.append(RemoteProviderConfig.from_env(provider_id))
            except Exception as exc:
                # A misconfigured optional provider must not break the server;
                # it is simply not registered (and reported as unavailable).
                self._count("remote_provider_refused")
                self._start_error = ""
                _ = exc

        from forge.models.telemetry import Telemetry

        return build_inference_fabric(
            governor=governor, runtime_config=config,
            telemetry=Telemetry(enabled=True),
            reference_dirs=tuple(self.config.reference_dirs),
            allow_network=bool(self.config.allow_network),
            remote_configs=tuple(remotes),
            max_resident_bytes=int(self.config.max_resident_bytes),
            max_slots=max(1, int(self.config.max_model_slots)),
            idle_seconds=float(self.config.idle_unload_seconds),
            auto_verify=bool(self.config.auto_verify))

    def start(self) -> "ServerInferenceService":
        """Eagerly build the fabric (idempotent). Never raises when disabled."""
        if not self.config.enabled:
            return self
        try:
            _ = self.fabric
        except InferenceNotConfigured:
            pass
        return self

    def stop(self) -> None:
        with self._lock:
            fabric = self._fabric
            self._streams.clear()
        if fabric is not None:
            try:
                fabric.cancel_all("server-shutdown")
            except Exception:
                pass
            runtime = getattr(getattr(fabric, "routing", None), "catalog", None)
            _ = runtime
        self._started = False

    # -- model operations -------------------------------------------------

    def models_list(self, *, backend_id: str = "", capability: str = "",
                    usable_only: bool = False,
                    discover: bool = False) -> Dict[str, Any]:
        fabric = self.fabric
        payload = fabric.models_list(backend_id=backend_id,
                                     capability=capability,
                                     usable_only=usable_only,
                                     discover=discover)
        payload["require_verified"] = bool(self.config.require_verified)
        payload["service"] = self.status()
        return payload

    def models_status(self, model_id: str = "") -> Dict[str, Any]:
        fabric = self.fabric
        payload = fabric.models_status(model_id)
        payload["service"] = self.status()
        return payload

    def models_verify(self, model_id: str = "", *,
                      backend_id: str = "") -> Dict[str, Any]:
        fabric = self.fabric
        payload = fabric.models_verify(model_id, backend_id=backend_id)
        self._audit("models.verify", scope=model_id or "*",
                    allowed=bool(payload.get("verified")),
                    reason=("%d/%d verified"
                            % (payload.get("verified", 0),
                               payload.get("count", 0))))
        payload["service"] = self.status()
        return payload

    def models_load(self, model_id: str, *,
                    timeout: Optional[float] = None) -> Dict[str, Any]:
        if not model_id:
            raise InvalidRequest("model_id is required")
        fabric = self.fabric
        payload = fabric.models_load(model_id, timeout=timeout)
        allowed = bool(payload.get("loaded"))
        self._audit("models.load", scope=model_id, allowed=allowed,
                    reason=str(payload.get("error") or "loaded")[:300])
        self._count("load" if allowed else "load_refused")
        return payload

    def models_unload(self, model_id: str, *,
                      force: bool = False) -> Dict[str, Any]:
        if not model_id:
            raise InvalidRequest("model_id is required")
        payload = self.fabric.models_unload(model_id, force=force)
        self._audit("models.unload", scope=model_id,
                    allowed=bool(payload.get("unloaded")),
                    reason=str(payload.get("reason") or "unloaded")[:300])
        return payload

    # -- inference --------------------------------------------------------

    def inference_generate(self, payload: Dict[str, Any], *,
                           actor: str = "") -> Dict[str, Any]:
        """One bounded, policy-checked generation. Typed inputs only."""
        fabric = self.fabric
        request = self._request_from(payload)
        task_id = str(payload.get("task_id") or "")[:128]
        attempt_id = str(payload.get("attempt_id") or "")[:128]
        classification = str(payload.get("classification") or "")[:16]
        self._admit()
        try:
            result = fabric.generate(request, task_id=task_id,
                                     attempt_id=attempt_id,
                                     classification=classification,
                                     fence=self._fence_for(task_id),
                                     fence_registry=self.fences)
        finally:
            self._release()
        body = self._result_body(result)
        body["text"] = _bounded_text(getattr(result, "text", ""),
                                     MAX_STREAM_CHARS)
        self._audit("inference.generate",
                    scope=str(getattr(result, "model_id", "") or "-"),
                    allowed=bool(getattr(result, "success", False)),
                    reason=str(getattr(result, "error", "")
                               or getattr(result, "state", ""))[:300],
                    task_id=task_id)
        self._count("generate")
        if not getattr(result, "success", False):
            self._count("generate_failed")
        self._notify(task_id, "inference.completed", body)
        return body

    def inference_stream(self, payload: Dict[str, Any], *,
                         actor: str = "") -> Dict[str, Any]:
        """Start a bounded stream; returns the stream id and first events."""
        fabric = self.fabric
        request = self._request_from(payload)
        task_id = str(payload.get("task_id") or "")[:128]
        attempt_id = str(payload.get("attempt_id") or "")[:128]
        classification = str(payload.get("classification") or "")[:16]
        self._admit()
        handle = fabric.stream(request, task_id=task_id,
                               attempt_id=attempt_id,
                               classification=classification,
                               fence=self._fence_for(task_id),
                               fence_registry=self.fences)
        stream_id = "str-" + handle.result.request_id[4:]
        retained = _RetainedStream(
            stream_id=stream_id, request_id=handle.result.request_id)
        with self._lock:
            self._gc_streams()
            self._streams[stream_id] = retained

        def _pump() -> None:
            try:
                for event in handle.events():
                    retained.append(event)
                result = handle.wait(1.0)
                retained.model_id = getattr(result, "model_id", "")
                retained.backend_id = getattr(result, "backend_id", "")
                retained.summary = {
                    key: value for key, value in
                    self._result_body(result).items()
                    if key in _STREAM_SUMMARY_KEYS}
                try:
                    retained.stream_snapshot = dict(handle.stream.snapshot())
                except Exception:
                    retained.stream_snapshot = {}
                retained.finish(state=str(getattr(result, "state", "")),
                                error=str(getattr(result, "error", "")),
                                error_code=str(getattr(result, "error_code",
                                                       "")))
                self._release()
                self._audit("inference.stream",
                            scope=retained.model_id or "-",
                            allowed=bool(getattr(result, "success", False)),
                            reason=str(getattr(result, "state", ""))[:300],
                            task_id=task_id)
                body = self._result_body(result)
                body["stream_id"] = stream_id
                self._notify(task_id, "inference.stream_completed", body)
            except Exception as exc:
                self._release()
                retained.finish(state="failed", error=_redact(str(exc)),
                                error_code="STREAM_FAILED")

        thread = threading.Thread(target=_pump,
                                  name="forge-server-inference-stream",
                                  daemon=True)
        thread.start()
        self._count("stream")
        # Give the producer a brief moment so the first response usually
        # carries real events (a thin client sees progress immediately).
        events, after, done = self._wait_events(retained, 0, wait=0.25)
        return {
            "stream_id": stream_id,
            "request_id": handle.result.request_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "model_id": handle.result.model_id,
            "backend_id": handle.result.backend_id,
            "events": events,
            "after": after,
            "done": done,
            "stream": handle.stream.snapshot(),
        }

    def inference_stream_events(self, stream_id: str, *, after: int = 0,
                                wait: float = 0.0) -> Dict[str, Any]:
        """Long-poll a stream from a cursor (exact replay, bounded)."""
        with self._lock:
            retained = self._streams.get(stream_id)
        if retained is None:
            raise NotFound("unknown or expired stream %r" % stream_id[:64])
        events, last, done = self._wait_events(retained, after, wait=wait)
        return {
            "stream_id": stream_id,
            "request_id": retained.request_id,
            "model_id": retained.model_id,
            "backend_id": retained.backend_id,
            "events": events,
            "after": last,
            "done": bool(done),
            "state": retained.state,
            "error": retained.error,
            "error_code": retained.error_code,
            "dropped_events": retained.dropped,
            "result": dict(retained.summary) if retained.done else {},
            "stream": (dict(retained.stream_snapshot)
                       if retained.done and retained.stream_snapshot else {}),
        }

    def inference_cancel(self, request_id: str, *,
                         reason: str = "operator") -> Dict[str, Any]:
        if not request_id:
            raise InvalidRequest("request_id is required")
        fabric = self.fabric
        signalled = bool(fabric.cancel(request_id[:128],
                                       reason=str(reason or "operator")[:64]))
        if not signalled:
            #: Nothing was in flight, so no producer will ever close a
            #: retained stream for this request: close it here instead of
            #: leaving a client polling an open stream forever. When the
            #: cancellation *was* delivered, the producer owns the ending --
            #: finishing it here would publish ``done`` before the pump had
            #: recorded the provenance of the attempt it just stopped.
            with self._lock:
                for retained in self._streams.values():
                    if retained.request_id == request_id[:128] and \
                            not retained.done:
                        retained.finish(state="cancelled",
                                        error="cancelled by operator",
                                        error_code="cancelled")
        self._audit("inference.cancel", scope=request_id[:128],
                    allowed=signalled,
                    reason="signalled" if signalled else "not in flight")
        self._count("cancel")
        return {"cancelled": signalled, "request_id": request_id[:128],
                "reason": str(reason or "operator")[:64]}

    def inference_status(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"service": self.status()}
        if self._fabric is not None:
            try:
                payload["fabric"] = self._fabric.status()
                payload["evidence"] = self._fabric.evidence(limit=50)
                payload["in_flight"] = self._fabric.in_flight()
            except Exception as exc:
                payload["error"] = _redact(str(exc))
        return payload

    def status(self) -> Dict[str, Any]:
        with self._lock:
            counts = dict(self._counts)
            streams = len(self._streams)
            running = self._running
        return {
            "enabled": bool(self.config.enabled),
            "started": bool(self._started),
            "start_error": self._start_error[:300],
            "config": self.config.to_dict(),
            "running_requests": running,
            "max_concurrent_requests": int(
                self.config.max_concurrent_requests),
            "retained_streams": streams,
            "counts": counts,
            "governor": _governor_snapshot(self.governor),
        }

    # -- internals --------------------------------------------------------

    def _request_from(self, payload: Dict[str, Any]) -> Any:
        """Validate and bound a typed request payload (never a command)."""
        from forge.models.request import ModelRequest

        if not isinstance(payload, dict):
            raise InvalidRequest("a request object is required")
        prompt = str(payload.get("prompt") or "")
        if not prompt.strip():
            raise InvalidRequest("prompt is required")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise InvalidRequest(
                "prompt exceeds the %d-character bound" % MAX_PROMPT_CHARS)
        context = str(payload.get("context") or "")[:MAX_CONTEXT_CHARS]
        #: An empty capability means "no capability requirement". Defaulting
        #: to a real capability here would silently turn the router's honest
        #: capability filter into a refusal for models that do not advertise
        #: it, so the caller's own (possibly empty) value is what counts.
        capability = str(payload.get("capability") or "")[:32]
        required = payload.get("required_capabilities") or ()
        if not isinstance(required, (list, tuple)):
            raise InvalidRequest("required_capabilities must be a list")
        try:
            max_output = payload.get("max_output_tokens")
            max_output = (None if max_output is None
                          else max(1, min(int(max_output), MAX_OUTPUT_TOKENS)))
            timeout = payload.get("timeout")
            timeout = (None if timeout is None else max(
                0.5, min(float(timeout),
                         min(MAX_TIMEOUT_SECONDS,
                             float(self.config.default_timeout_seconds)))))
        except (TypeError, ValueError):
            raise InvalidRequest(
                "max_output_tokens and timeout must be numbers") from None
        temperature = payload.get("temperature")
        if temperature is not None:
            try:
                temperature = max(0.0, min(2.0, float(temperature)))
            except (TypeError, ValueError):
                raise InvalidRequest("temperature must be a number") from None
        model = str(payload.get("model") or "")[:200]
        backend = str(payload.get("backend") or "")[:64]
        return ModelRequest(
            prompt=prompt, context=context, capability=capability,
            required_capabilities=tuple(str(item)[:32] for item in required),
            task=str(payload.get("task") or "")[:512],
            min_context_window=max(0, min(int(payload.get("min_context_window")
                                              or 0), 10 ** 7)),
            max_output_tokens=max_output,
            complexity=max(0.0, min(100.0, float(payload.get("complexity")
                                                 or 1.0))),
            temperature=temperature,
            model=model, backend=backend,
            timeout=timeout or self.config.default_timeout_seconds,
            classification=str(payload.get("classification") or "")[:16],
            network_policy=str(payload.get("network_policy") or "")[:16],
            hardware_profile=str(payload.get("hardware_profile") or "")[:32],
            require_verified=bool(self.config.require_verified),
            allow_deterministic=bool(
                payload.get("allow_deterministic", True)),
            task_id=str(payload.get("task_id") or "")[:128],
            attempt_id=str(payload.get("attempt_id") or "")[:128],
            trace_id=str(payload.get("trace_id") or "")[:64],
        )

    def _fence_for(self, task_id: str) -> Any:
        """The task's current attempt fence, when an authority is wired."""
        if self.fences is None or not task_id:
            return None
        try:
            return self.fences.current(task_id)
        except Exception:
            #: An unreadable fence authority must not turn into a fabricated
            #: authorization: no fence object means the fabric simply has no
            #: generation to check, exactly as before.
            return None

    def _admit(self) -> None:
        """Bounded concurrency: refuse rather than queue without limit."""
        acquired = self._semaphore.acquire(timeout=0.05)
        if not acquired:
            self._count("concurrency_refused")
            raise PermissionDenied(
                "server inference concurrency limit reached (%d running)"
                % self._running)
        with self._lock:
            self._running += 1

    def _release(self) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)
        try:
            self._semaphore.release()
        except ValueError:
            pass

    def _wait_events(self, retained: _RetainedStream, after: int, *,
                     wait: float = 0.0) -> Tuple[List[Dict[str, Any]], int,
                                                 bool]:
        deadline = time.monotonic() + max(0.0, min(float(wait or 0.0),
                                                   25.0))
        with retained.notify:
            while True:
                events, last, done = retained.since(after)
                if events or done or time.monotonic() >= deadline:
                    return events, last, done
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return events, last, done
                retained.notify.wait(min(remaining, 0.5))

    def _gc_streams(self) -> None:
        cutoff = time.time() - STREAM_TTL_SECONDS
        expired = [key for key, value in self._streams.items()
                   if value.done and value.finished_at and
                   value.finished_at < cutoff]
        for key in expired:
            self._streams.pop(key, None)
        while len(self._streams) > MAX_RETAINED_STREAMS:
            oldest = min(self._streams.items(),
                         key=lambda kv: kv[1].created_at)[0]
            self._streams.pop(oldest, None)

    def _result_body(self, result: Any) -> Dict[str, Any]:
        body = result.to_dict()
        # A server response never carries the internal observation list verbatim
        # (bounded here), and never carries prompt/context content.
        body["observations"] = [item for item in body.get("observations", [])
                                ][-12:]
        body.pop("metadata", None)
        return body

    def _audit(self, operation: str, *, scope: str, allowed: bool,
               reason: str = "", task_id: str = "") -> None:
        if self.audit is None:
            return
        try:
            from forge.security.policy_gate import PolicyDecision
            self.audit.record_decision(
                agent="forge-server-inference", resource="model",
                operation=operation, scope=scope[:200],
                decision=(PolicyDecision.ALLOW if allowed
                          else PolicyDecision.DENY),
                reason=reason[:300], task_id=task_id)
        except Exception:
            pass

    def _notify(self, task_id: str, event_type: str,
                payload: Dict[str, Any]) -> None:
        if self._emit is None or not task_id:
            return
        try:
            bounded = {key: payload[key] for key in
                       ("request_id", "model_id", "backend_id", "state",
                        "success", "error_code", "latency_ms", "neural")
                       if key in payload}
            self._emit(task_id, "", event_type, bounded)
        except Exception:
            pass

    def _count(self, key: str) -> None:
        with self._lock:
            self._counts[key] = int(self._counts.get(key, 0)) + 1


def _tighten_governor(governor: Any, requested: str, *,
                      count: Optional[Callable[[str], None]] = None) -> Any:
    """Apply an inference-specific resource profile — but only if stricter.

    The server's :class:`ResourceGovernor` is authoritative for the process.
    An operator may tighten it for inference (for example pinning the service
    to the ``g560`` profile so it can never load a model); nothing may loosen
    it. A request that is not strictly tighter is ignored and counted, so the
    refusal is visible in ``inference.status`` instead of silently widening a
    budget.
    """
    from forge.core.resource_governor import ResourceGovernor, profile_from_name

    name = str(requested or "").strip()
    if not name:
        return governor
    current = getattr(governor, "profile", None)
    candidate = profile_from_name(name)
    if current is None or _is_stricter(candidate, current):
        if count is not None:
            count("resource_profile_applied")
        return ResourceGovernor(candidate)
    if count is not None:
        count("resource_profile_ignored")
    return governor


def _is_stricter(candidate: Any, current: Any) -> bool:
    """True when ``candidate``'s budget is tighter on any dimension."""
    try:
        left, right = candidate.budget, current.budget
    except AttributeError:
        return False
    if bool(left.model_loading_allowed) != bool(right.model_loading_allowed):
        # Denying local model loading outright is the tighter verdict.
        return not bool(left.model_loading_allowed)
    if int(left.max_workers) != int(right.max_workers):
        return int(left.max_workers) < int(right.max_workers)
    #: ``model_memory_mb == 0`` means "no local model memory at all".
    if int(left.model_memory_mb) != int(right.model_memory_mb):
        return int(left.model_memory_mb) < int(right.model_memory_mb)
    #: ``max_cost_usd == 0`` means "no explicit cost bound".
    left_cost = float(left.max_cost_usd) or float("inf")
    right_cost = float(right.max_cost_usd) or float("inf")
    if left_cost != right_cost:
        return left_cost < right_cost
    #: ``off`` is the tightest network posture.
    rank = {"off": 0, "server-only": 1, "explicit": 2}
    left_net = rank.get(str(left.network_policy), 0)
    right_net = rank.get(str(right.network_policy), 0)
    if left_net != right_net:
        return left_net < right_net
    return False


def _bounded_text(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit]


def _governor_snapshot(governor: Any) -> Dict[str, Any]:
    if governor is None:
        return {}
    try:
        snapshot = governor.snapshot()
        profile = snapshot.get("profile") or {}
        return {
            "profile": str(profile.get("name") or ""),
            "model_loading_allowed": bool(
                profile.get("model_loading_allowed", True)),
            "network_policy": str(profile.get("network_policy") or ""),
            "max_workers": int(profile.get("max_workers") or 0),
            "usage": dict(snapshot.get("usage") or {}),
        }
    except Exception:
        return {}


def _redact(text: str) -> str:
    try:
        from forge.runtime.model_runtime import redact_text
        return redact_text(str(text or ""))[:400]
    except Exception:
        return str(text or "")[:400]
