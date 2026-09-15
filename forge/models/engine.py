"""The inference fabric service (Session 11).

:class:`InferenceFabric` is the request path that ties the Session 11 pieces
together, without replacing any of them::

    Agent -> InferenceFabric
               -> RoutingEngine      (policy + resource aware, explainable)
               -> ModelCatalog       (one canonical registry)
               -> ModelResidencyCache(bounded, refcounted, single-flight)
               -> Backend            (stable protocol)
                  -> ModelRuntime    (Session 10 canonical runtime)
                     -> ModelBackend -> model
               -> ContextBudgetPlanner (deterministic context)
               -> FenceRegistry        (stale attempts cannot publish)
               -> Telemetry            (metadata only)

What it guarantees
------------------
* **Honest terminal states.** Every result carries a
  :class:`~forge.models.fallback.TerminalState`: ``succeeded``,
  ``needs_model``, ``policy_denied``, ``resource_denied``, ``timeout``,
  ``cancelled``, ``stale``, ``unverified``, ``failed``. A provider failure,
  a network failure, a timeout or an auth failure stays a failure.
* **No fabrication.** Text is only returned when a backend produced it. The
  deterministic rung is labelled ``neural=False`` and reuses the existing
  offline no-op provider, which itself refuses to synthesize code.
* **Fenced.** A generation carries ``task_id`` / ``attempt_id`` /
  ``generation_id`` / ``model_id`` / ``backend_id``; a result whose fence is no
  longer authorized is discarded as ``stale`` and its text is dropped.
* **Bounded.** Context, output characters, output tokens, timeout, stream
  buffer and stream length are all bounded before the call, not after.
* **Untrusted output.** Model text never becomes a command, a permission, an
  approval or a path. :func:`scan_model_output` only *reports* what the text
  looks like; the canonical policy/tool layers still decide everything.
* **Secret-safe.** No prompt, context or completion text reaches telemetry,
  logs, events or exceptions; every error string is redacted.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterator, List, Optional, Sequence,
                    Tuple)
from uuid import uuid4

from forge.models.backends import (Backend, BackendError, BackendRegistry,
                                   NativeLocalBackend, OllamaCompatibleBackend,
                                   LlamaCppCompatibleBackend,
                                   ForgeCustomBackend, RemoteProviderBackend,
                                   RuntimeBackendAdapter)
from forge.models.catalog import CatalogError, ModelCatalog
from forge.models.context_budget import ContextBudgetPlanner, ContextPlan
from forge.models.fallback import (FallbackLadder, FallbackPlan, FallbackStep,
                                   TerminalState, classify_error)
from forge.models.identity import (AvailabilityState, ModelIdentity,
                                   ModelSpoofingError, VerificationState)
from forge.models.model_cache import ModelResidencyCache, ModelResidencyError
from forge.models.routing import (RoutingEngine, RoutingPlan, RoutingRequest,
                                  RoutingState)
from forge.models.streams import BoundedStream, StreamEvent, join_deltas
from forge.models.verification import ModelVerifier, VerificationResult

__all__ = [
    "InferenceFabric",
    "InferenceResult",
    "InferenceStreamHandle",
    "Observation",
    "build_inference_fabric",
    "scan_model_output",
]

#: Hard ceiling on characters accepted from one generation. The model's own
#: token bound applies first; this stops an unbounded answer regardless.
MAX_OUTPUT_CHARS = 256 * 1024
#: Delta size used when streaming the non-neural rung (bounded, honest).
_DETERMINISTIC_CHUNK_CHARS = 48
#: Bound on how much output is inspected for injection-shaped text.
SCAN_CHAR_BOUND = 64 * 1024

#: ``auto_verify`` proves at most this many models before one request, so
#: a discovery sweep cannot turn into an unbounded verification storm.
AUTO_VERIFY_MAX = 4

_INJECTION_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("instruction_override", re.compile(
        r"(?i)(ignore|disregard|forget)\s+(all\s+|the\s+)?"
        r"(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)")),
    ("role_hijack", re.compile(
        r"(?i)^\s*(system|assistant|tool)\s*:", re.MULTILINE)),
    ("shell_command", re.compile(
        r"(?m)^\s*(\$|#)\s*(rm|curl|wget|sh|bash|powershell|cmd|sudo|"
        r"chmod|chown|nc|ssh|scp|dd|mkfs|reg|net)\b")),
    ("permission_grant", re.compile(
        r"(?i)\b(grant|approve|allow|authorize)\s+"
        r"(this|the)\s+(request|permission|access|tool|approval)\b")),
    ("credential_request", re.compile(
        r"(?i)\b(send|provide|paste|share)\b[^.\n]{0,40}"
        r"\b(api[_ -]?key|token|password|secret|credential)\b")),
    ("tool_invocation", re.compile(
        r"(?i)\b(call|invoke|run|execute)\s+tool\s*[:#]")),
    ("exfiltration_url", re.compile(
        r"(?i)\b(fetch|post|upload|send)\b[^.\n]{0,40}"
        r"https?://[^\s\"')]+")),
)


def _honest_error_code(state: str, code: str) -> str:
    """Name the failure after its cause, not after where it surfaced.

    A backend that raises because the caller withdrew the attempt is not a
    backend failure: reporting ``BACKEND_ERROR`` for a cancellation sends
    operators after the wrong thing. The terminal state is already decided;
    this only keeps the code consistent with it.
    """
    if state == TerminalState.CANCELLED.value and code not in (
            "CANCELLED", "STALE", "STREAM_TIMEOUT"):
        return "CANCELLED"
    if state == TerminalState.TIMEOUT.value and code in ("BACKEND_ERROR", ""):
        return "TIMEOUT"
    return code or "FAILED"


def scan_model_output(text: str) -> Dict[str, Any]:
    """Report what untrusted model output looks like. Never acts on it.

    The result is *evidence for humans and for the policy layers*: nothing
    here grants, denies, executes, or authorizes anything.
    """
    sample = (text or "")[:SCAN_CHAR_BOUND]
    flags: List[str] = []
    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(sample):
            flags.append(name)
    return {
        "scanned_chars": len(sample),
        "flags": flags,
        "suspicious": bool(flags),
        "note": ("model output is untrusted data; flags are informational and "
                 "never authorize an action") if flags else "",
    }


@dataclass
class Observation:
    """One phase of an inference execution (observability, §28)."""

    phase: str
    state: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    detail: str = ""
    error_code: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return max(0.0, (end - self.started_at) * 1000.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 3),
            "detail": self.detail[:400],
            "error_code": self.error_code,
            "metadata": dict(self.metadata),
        }


@dataclass
class InferenceResult:
    """The full, honest outcome of one inference request."""

    request_id: str = ""
    task_id: str = ""
    attempt_id: str = ""
    generation_id: str = ""
    model_id: str = ""
    backend_id: str = ""
    provider: str = ""
    text: str = ""
    success: bool = False
    state: str = TerminalState.FAILED.value
    error: str = ""
    error_code: str = ""
    finish_reason: str = ""
    #: True only when a real model produced ``text``.
    neural: bool = True
    #: True when a stream reached ``done=True`` without error.
    done: bool = False
    streamed: bool = False
    truncated: bool = False
    latency_ms: float = 0.0
    time_to_first_token_ms: Optional[float] = None
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    phase: str = "complete"
    availability_state: str = ""
    verification_state: str = ""
    routing: Dict[str, Any] = field(default_factory=dict)
    policy_result: Dict[str, Any] = field(default_factory=dict)
    resource_result: Dict[str, Any] = field(default_factory=dict)
    fallback: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    output_scan: Dict[str, Any] = field(default_factory=dict)
    observations: List[Observation] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def terminal_state(self) -> str:
        return self.state

    def observe(self, phase: str, *, state: str = "", detail: str = "",
                error_code: str = "", started_at: Optional[float] = None,
                **metadata: Any) -> Observation:
        observation = Observation(
            phase=phase, state=state or phase,
            started_at=started_at if started_at is not None else time.time(),
            finished_at=time.time(), detail=detail[:400],
            error_code=error_code, metadata=dict(metadata))
        self.observations.append(observation)
        if len(self.observations) > 64:
            self.observations = self.observations[-64:]
        self.phase = phase
        return observation

    def to_model_response(self) -> Any:
        """Legacy ``ModelResponse`` view for existing fabric consumers."""
        from forge.models.inference_path import PATH_SESSION11
        from forge.models.request import ModelResponse

        response = ModelResponse(
            text=self.text, model=self.model_id, provider=self.provider,
            success=bool(self.success), error=self.error,
            request_id=self.request_id, input_tokens=self.input_tokens,
            output_tokens=self.output_tokens, latency_ms=self.latency_ms,
            finish_reason=self.finish_reason or
            ("stop" if self.success else "error"))
        response.metadata.update({
            "backend_id": self.backend_id,
            "state": self.state,
            "error_code": self.error_code,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "generation_id": self.generation_id,
            "neural": bool(self.neural),
            "done": bool(self.done),
            "streamed": bool(self.streamed),
            "truncated": bool(self.truncated),
            "time_to_first_token_ms": self.time_to_first_token_ms,
            "availability_state": self.availability_state,
            "verification_state": self.verification_state,
            "fallback_used": bool(self.fallback.get("fallback_used")),
            "output_flags": list(self.output_scan.get("flags") or ()),
            #: §20/§30 — the path labels itself. A response that came out of the
            #: Session-11 fabric says so even when the consumer never travelled
            #: the ModelFabric seam (an agent holding this fabric directly, the
            #: CLI, a benchmark). Without this, the honest fallback in
            #: ``provenance_from_response`` would have to call it legacy — a
            #: false label for work this fabric really did.
            "inference_path": PATH_SESSION11,
        })
        return response

    def to_dict(self, *, include_text: bool = False) -> Dict[str, Any]:
        """A loggable view: identifiers, timings and decisions — no content."""
        from forge.models.inference_path import PATH_SESSION11

        payload: Dict[str, Any] = {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "generation_id": self.generation_id,
            "model_id": self.model_id,
            "backend_id": self.backend_id,
            "provider": self.provider,
            "success": bool(self.success),
            "state": self.state,
            "neural": bool(self.neural),
            "done": bool(self.done),
            "streamed": bool(self.streamed),
            "truncated": bool(self.truncated),
            "error": self.error[:400],
            "error_code": self.error_code,
            "finish_reason": self.finish_reason,
            "inference_path": PATH_SESSION11,
            "phase": self.phase,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "latency_ms": round(float(self.latency_ms or 0.0), 3),
            "time_to_first_token_ms": self.time_to_first_token_ms,
            "input_tokens": int(self.input_tokens or 0),
            "output_tokens": int(self.output_tokens or 0),
            "text_chars": len(self.text or ""),
            "availability_state": self.availability_state,
            "verification_state": self.verification_state,
            "routing": dict(self.routing),
            "policy_result": dict(self.policy_result),
            "resource_result": dict(self.resource_result),
            "fallback": dict(self.fallback),
            "verification": dict(self.verification),
            "context": dict(self.context),
            "output_scan": dict(self.output_scan),
            "observations": [item.to_dict() for item in self.observations],
            "metadata": dict(self.metadata),
        }
        if include_text:
            payload["text"] = self.text
        return payload


@dataclass
class StreamConfig:
    max_buffer: int = 64
    max_total_chars: int = 64 * 1024
    put_timeout: float = 5.0
    get_timeout: Optional[float] = None


class InferenceStreamHandle:
    """A running (or finished) bounded stream."""

    def __init__(self, stream: BoundedStream, result: InferenceResult,
                 thread: threading.Thread, *,
                 fabric: "InferenceFabric") -> None:
        self.stream = stream
        self.result = result
        self._thread = thread
        self._fabric = fabric

    def events(self) -> Iterator[StreamEvent]:
        return self.stream.events()

    def __iter__(self) -> Iterator[StreamEvent]:
        return self.events()

    def cancel(self, reason: str = "cancelled") -> bool:
        return self._fabric.cancel(self.result.request_id, reason=reason)

    def wait(self, timeout: Optional[float] = None) -> InferenceResult:
        """Block until the producer finishes and return the final result."""
        self._thread.join(timeout)
        return self.result

    def text(self) -> str:
        return self.stream.text()

    @property
    def done(self) -> bool:
        return self.stream.done

    @property
    def complete(self) -> bool:
        return self.stream.complete

    def snapshot(self) -> Dict[str, Any]:
        return {"stream": self.stream.snapshot(),
                "result": self.result.to_dict()}


class InferenceFabric:
    """The canonical inference request path."""

    def __init__(self, *, catalog: ModelCatalog,
                 routing: Optional[RoutingEngine] = None,
                 telemetry: Any = None,
                 planner: Optional[ContextBudgetPlanner] = None,
                 verifier: Optional[ModelVerifier] = None,
                 ladder: Optional[FallbackLadder] = None,
                 deterministic_provider: Any = None,
                 deterministic_identity: Optional[ModelIdentity] = None,
                 fence_registry: Any = None,
                 stream_config: Optional[StreamConfig] = None,
                 max_output_chars: int = MAX_OUTPUT_CHARS,
                 auto_verify: bool = False,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.catalog = catalog
        self.telemetry = telemetry
        self.planner = planner or ContextBudgetPlanner()
        self.verifier = verifier or catalog.verifier
        self.ladder = ladder or FallbackLadder()
        self.deterministic_provider = deterministic_provider
        self.deterministic_identity = deterministic_identity or _make_deterministic_identity()
        self.fence_registry = fence_registry
        self.stream_config = stream_config or StreamConfig()
        self.max_output_chars = max(64, int(max_output_chars or 64))
        #: Verify a model on first use (bounded, real). Off by default so an
        #: existing deployment is never slowed silently.
        self.auto_verify = bool(auto_verify)
        self.routing = routing or RoutingEngine(
            catalog, governor=catalog.governor, telemetry=telemetry,
            ladder=self.ladder,
            deterministic_identity=self.deterministic_identity)
        self._lock = threading.RLock()
        self._tokens: Dict[str, Any] = {}
        self._streams: Dict[str, BoundedStream] = {}
        self._results: Dict[str, InferenceResult] = {}
        self._history: List[Dict[str, Any]] = []
        self._clock = clock
        self._counts: Dict[str, int] = {}

    # -- construction ----------------------------------------------------

    @classmethod
    def from_runtime(cls, runtime: Any, *, governor: Any = None,
                     policy: Any = None, telemetry: Any = None,
                     data_policy: Any = None,
                     deterministic_provider: Any = None,
                     fence_registry: Any = None,
                     remote_configs: Sequence[Any] = (),
                     max_resident_bytes: int = 0, max_slots: int = 2,
                     idle_seconds: float = 0.0,
                     capabilities: Tuple[str, ...] = (),
                     **kwargs: Any) -> "InferenceFabric":
        """Build a fabric over an existing :class:`ModelRuntime`.

        Only backends the runtime actually has registered are exposed; a
        backend that reports unavailable stays registered but is never
        ``ready``.
        """
        from forge.runtime.model_runtime import ModelRuntime

        if runtime is None:
            raise ValueError("a ModelRuntime instance is required")
        backends = BackendRegistry()
        known = {info.name: info for info in runtime.backends()}

        def _register(adapter: Backend) -> None:
            if adapter.backend_name in known:
                backends.register(adapter)

        _register(NativeLocalBackend(runtime, capabilities=capabilities,
                                     governor=governor))
        _register(OllamaCompatibleBackend(
            runtime, capabilities=capabilities, governor=governor,
            base_url=str(getattr(getattr(runtime, "config", None),
                                 "ollama_url", "") or
                         "http://127.0.0.1:11434")))
        _register(LlamaCppCompatibleBackend(runtime, governor=governor,
                                            capabilities=capabilities))
        _register(ForgeCustomBackend(runtime, governor=governor,
                                     capabilities=capabilities))
        for name, info in sorted(known.items()):
            if name in ("native", "ollama", "llama_cpp", "forge"):
                continue
            # A custom backend registered on the runtime keeps its *own*
            # locality: the first-party reference engine is local and offline,
            # and labelling it remote would misreport network requirements,
            # cost posture and the honest backend table. Only a backend that
            # really declares itself non-local is wrapped as a remote provider.
            is_local = bool(getattr(info, "local", True))
            if is_local:
                backends.register(RuntimeBackendAdapter(
                    runtime, name, backend_id=name,
                    kind=str(getattr(info, "kind", "") or "custom"),
                    local=True,
                    requires_network=bool(getattr(info, "requires_network",
                                                  False)),
                    free=True, capabilities=capabilities, governor=governor,
                    description=str(getattr(info, "description", "") or "")))
            else:
                #: A remote provider carries its declared cost, so free-first
                #: routing can tell "free" from "the operator did not say".
                cost = 0.0
                try:
                    inner = runtime.get_backend(name)
                    cost = float(getattr(getattr(inner, "config", None),
                                         "cost_per_token", 0.0) or 0.0)
                except Exception:
                    cost = 0.0
                backends.register(RemoteProviderBackend(
                    runtime, name, backend_id=name, provider_id=name,
                    capabilities=capabilities, governor=governor,
                    cost_per_token=cost,
                    description=str(getattr(info, "description", "") or "")))

        cache = ModelResidencyCache(
            max_bytes=max_resident_bytes, max_slots=max_slots,
            idle_seconds=idle_seconds,
            clock=kwargs.pop("clock", time.monotonic))
        catalog = ModelCatalog(backends=backends, cache=cache,
                               governor=governor, telemetry=telemetry,
                               verifier=kwargs.pop("verifier", None))
        fabric = cls(catalog=catalog, telemetry=telemetry,
                     deterministic_provider=deterministic_provider,
                     fence_registry=fence_registry, **kwargs)
        fabric.routing = RoutingEngine(
            catalog, governor=governor, policy=policy, telemetry=telemetry,
            data_policy=data_policy, ladder=fabric.ladder,
            deterministic_identity=fabric.deterministic_identity)
        return fabric

    # -- model operations (typed, no arbitrary commands) ------------------

    def models_list(self, *, backend_id: str = "", capability: str = "",
                    usable_only: bool = False,
                    discover: bool = False) -> Dict[str, Any]:
        if discover:
            report = self.catalog.discover(backend_id=backend_id)
        else:
            report = None
        identities = self.catalog.list(backend_id=backend_id,
                                       capability=capability,
                                       usable_only=usable_only)
        return {
            "models": [identity.to_dict() for identity in identities],
            "count": len(identities),
            "backends": [status.to_dict()
                         for status in self.catalog.backends.statuses(
                             probe=False)],
            "discovery": report.to_dict() if report else None,
            "deterministic": self.deterministic_identity.to_dict(),
        }

    def models_status(self, model_id: str = "") -> Dict[str, Any]:
        try:
            return self.catalog.status(model_id)
        except CatalogError as exc:
            return {"error": exc.message, "error_code": exc.code,
                    "model_id": model_id}

    def models_verify(self, model_id: str = "", *,
                      backend_id: str = "") -> Dict[str, Any]:
        """Verify one model, or every registered model when none is named."""
        if model_id:
            result = self.catalog.verify(model_id, backend_id=backend_id)
            return {"results": [result.to_dict()],
                    "verified": result.verified,
                    "count": 1}
        results = self.catalog.verify_all(backend_id=backend_id)
        return {"results": [item.to_dict() for item in results],
                "verified": sum(1 for item in results if item.verified),
                "count": len(results)}

    def models_load(self, model_id: str, *, backend_id: str = "",
                    timeout: Optional[float] = None) -> Dict[str, Any]:
        try:
            return self.catalog.load(model_id, backend_id=backend_id,
                                     timeout=timeout)
        except CatalogError as exc:
            self._count("load_refused")
            return {"loaded": False, "model_id": model_id,
                    "error": exc.message, "error_code": exc.code}

    def models_unload(self, model_id: str, *,
                      force: bool = False) -> Dict[str, Any]:
        return self.catalog.unload(model_id, force=force)

    def backends(self, *, probe: bool = False) -> List[Dict[str, Any]]:
        return [status.to_dict()
                for status in self.catalog.backends.statuses(probe=probe)]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            counts = dict(self._counts)
            in_flight = len(self._tokens)
            recent = [dict(item) for item in self._history[-20:]]
        return {
            "counts": counts,
            "in_flight": in_flight,
            "models": self.catalog.status(),
            "backends": self.backends(probe=False),
            "recent": recent,
        }

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._history[-max(1, int(limit)):]]

    # -- generation ------------------------------------------------------

    def _auto_prepare(self, model_request: Any, result: InferenceResult) -> None:
        """``auto_verify``: prove a model *before* routing, never instead of.

        Selection refuses an unverified model, so verifying after a model was
        selected would be too late and the flag would be dead. This runs the
        catalog's real verification (artifact fingerprint + backend health +
        one probe generation) on the models this request could use. A model
        that fails stays unverified and is still refused; nothing here promotes
        a state on its own authority.
        """
        if not self.auto_verify:
            return
        wanted = str(getattr(model_request, "model", "") or "").strip()
        backend = str(getattr(model_request, "backend", "") or "").strip()
        capability = str(getattr(model_request, "capability", "") or "")
        #: A verification probe is real outbound traffic. When the request
        #: itself forbids leaving the machine (``network_policy=off``) or
        #: declares data that may not reach an external provider, no probe is
        #: sent to a non-local candidate: the request is refused by routing
        #: without Forge having talked to the provider first.
        network_policy = str(getattr(model_request, "network_policy", "")
                             or "").strip().lower()
        classification = str(getattr(model_request, "classification", "")
                             or "").strip().lower()
        outbound_forbidden = (network_policy == "off"
                              or classification in ("secret", "confidential"))
        if wanted:
            targets = [wanted]
        else:
            try:
                targets = [identity.model_id
                           for identity in self.catalog.list(
                               backend_id=backend, capability=capability)
                           if not identity.verified][:AUTO_VERIFY_MAX]
            except Exception:
                targets = []
        attempted = 0
        verified = 0
        for model_id in targets:
            try:
                identity = self.catalog.get(model_id)
            except Exception:
                continue
            if identity.verified:
                continue
            if outbound_forbidden and not bool(identity.local):
                result.observe(
                    "auto_verify", state=TerminalState.POLICY_DENIED.value,
                    detail=("auto-verification skipped for the non-local "
                            "model %s: this request permits no outbound "
                            "traffic (network_policy=%s, classification=%s)"
                            % (model_id, network_policy or "default",
                               classification or "unset")),
                    error_code="NETWORK_POLICY", model_id=model_id)
                continue
            try:
                outcome = self.catalog.verify(model_id)
            except Exception as exc:      # routing still refuses it honestly
                result.observe("auto_verify",
                               state=TerminalState.UNVERIFIED.value,
                               detail="auto-verification of %s failed: %s"
                                      % (model_id, str(exc)[:200]),
                               error_code="VERIFICATION",
                               model_id=model_id)
                continue
            attempted += 1
            verified += 1 if outcome.verified else 0
            result.observe(
                "auto_verify",
                state=(TerminalState.SUCCEEDED.value if outcome.verified
                       else TerminalState.UNVERIFIED.value),
                detail="auto-verified %s: verified=%s method=%s"
                       % (model_id, outcome.verified, outcome.method or "-"),
                error_code="" if outcome.verified else (
                    outcome.error_code or "UNVERIFIED"),
                model_id=model_id, verified=bool(outcome.verified),
                method=str(outcome.method or ""))
        if attempted:
            result.metadata["auto_verify"] = {"attempted": attempted,
                                              "verified": verified}

    def generate(self, request: Any, *, task_id: str = "", attempt_id: str = "",
                 fence: Any = None, fence_registry: Any = None,
                 commit_guard: Any = None,
                 classification: str = "", stream: bool = False,
                 generation_id: str = "") -> Any:
        """Run one bounded, fenced, policy-checked generation."""
        from forge.models.request import ModelRequest
        from forge.runtime.model_runtime import RuntimeRequest

        model_request = _coerce_request(request, ModelRequest)
        if stream:
            return self.stream(model_request, task_id=task_id,
                               attempt_id=attempt_id, fence=fence,
                               fence_registry=fence_registry,
                               commit_guard=commit_guard,
                               classification=classification,
                               generation_id=generation_id)

        result = self._new_result(model_request, task_id=task_id,
                                  attempt_id=attempt_id, fence=fence,
                                  generation_id=generation_id)
        started = self._clock()
        token = self._register_token(result.request_id)
        guard = commit_guard if commit_guard is not None else (
            _guard_from(fence, fence_registry or self.fence_registry)
            if fence is not None else None)
        try:
            self._auto_prepare(model_request, result)
            plan = self._route(model_request, result, task_id=task_id,
                               attempt_id=attempt_id,
                               classification=classification)
            if plan is None:
                return self._finish(result, started)

            # Fence check *before* spending any compute.
            stale = self._fence_reason(guard, result)
            if stale:
                return self._stale(result, started, stale)

            ladder = plan.ladder
            steps = list(ladder.steps) if ladder is not None else []
            last_error = ""
            last_code = plan.error_code
            for index, step in enumerate(steps):
                if token.cancelled:
                    break
                step.attempted = True
                step.started_at = time.time()
                if not step.neural:
                    outcome = self._run_deterministic(model_request, result,
                                                      step)
                else:
                    outcome = self._run_step(model_request, result, step,
                                             plan, token, guard)
                step.latency_ms = outcome.get("latency_ms", 0.0)
                step.error = outcome.get("error", "")
                step.error_code = outcome.get("error_code", "")
                step.outcome = outcome.get("state", TerminalState.FAILED.value)
                if outcome.get("success"):
                    #: Serving from the non-neural rung *is* a fallback, even
                    #: when it is the first (or only) step: no model answered.
                    used = bool(index > 0 or not step.neural)
                    ladder.accepted_step = step.order
                    ladder.terminal_state = TerminalState.SUCCEEDED.value
                    ladder.fallback_used = used
                    result.fallback = {
                        "used": used, "steps": index + 1,
                        "tier": step.tier, "neural": bool(step.neural),
                        "ladder": ladder.to_dict(),
                    }
                    return self._finish(result, started)
                last_error = outcome.get("error", "") or last_error
                last_code = outcome.get("error_code", "") or last_code
                if outcome.get("state") in (TerminalState.CANCELLED.value,
                                            TerminalState.STALE.value,
                                            TerminalState.TIMEOUT.value):
                    # A cancellation, a fence or a deadline is terminal: the
                    # next rung would only burn the budget of a dead attempt.
                    break
                if outcome.get("state") == TerminalState.POLICY_DENIED.value \
                        and not step.neural:
                    break
            state = classify_error(last_error, code=last_code,
                                   fenced=bool(result.metadata.get("fenced")))
            if ladder is not None and ladder.terminal_state in (
                    "", TerminalState.NEEDS_MODEL.value):
                ladder.terminal_state = state
                ladder.fallback_used = bool(ladder.attempted)
            result.state = state
            result.success = False
            result.error = last_error or plan.reason or "no model could serve"
            result.error_code = last_code or "NO_MODEL"
            result.fallback = {"used": False,
                               "ladder": ladder.to_dict() if ladder else None}
            result.observe("ladder_exhausted", state=state,
                           detail=result.error, error_code=result.error_code)
            return self._finish(result, started)
        finally:
            self._unregister_token(result.request_id)

    def stream(self, request: Any, *, task_id: str = "", attempt_id: str = "",
               fence: Any = None, fence_registry: Any = None,
               commit_guard: Any = None, classification: str = "",
               generation_id: str = "") -> InferenceStreamHandle:
        """Start a bounded stream. The producer runs on a daemon thread."""
        from forge.models.request import ModelRequest

        model_request = _coerce_request(request, ModelRequest)
        result = self._new_result(model_request, task_id=task_id,
                                  attempt_id=attempt_id, fence=fence,
                                  generation_id=generation_id)
        result.streamed = True
        config = self.stream_config
        stream = BoundedStream(
            result.request_id, model_id=result.model_id,
            backend_id=result.backend_id, max_buffer=config.max_buffer,
            max_total_chars=config.max_total_chars,
            put_timeout=config.put_timeout, get_timeout=config.get_timeout)
        guard = commit_guard if commit_guard is not None else (
            _guard_from(fence, fence_registry or self.fence_registry)
            if fence is not None else None)
        with self._lock:
            self._streams[result.request_id] = stream
        thread = threading.Thread(
            target=self._produce_stream,
            args=(model_request, result, stream, guard, classification),
            name="forge-inference-stream", daemon=True)
        handle = InferenceStreamHandle(stream, result, thread, fabric=self)
        thread.start()
        return handle

    # -- cancellation ----------------------------------------------------

    def cancel(self, request_id: str, *, reason: str = "cancelled") -> bool:
        """Cancel one in-flight request (generation or stream)."""
        with self._lock:
            token = self._tokens.get(request_id)
            stream = self._streams.get(request_id)
        signalled = False
        if token is not None:
            try:
                signalled = bool(token.cancel(reason))
            except Exception:
                signalled = False
        if stream is not None:
            stream.cancel(reason)
            signalled = True
        if signalled:
            self._count("cancelled")
        return signalled

    def cancel_all(self, reason: str = "shutdown") -> int:
        with self._lock:
            ids = list(self._tokens)
        return sum(1 for request_id in ids
                   if self.cancel(request_id, reason=reason))

    def in_flight(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{"request_id": key,
                     "task_id": value.task_id,
                     "attempt_id": value.attempt_id,
                     "model_id": value.model_id,
                     "backend_id": value.backend_id,
                     "started_at": value.started_at}
                    for key, value in sorted(self._results.items())
                    if key in self._tokens]

    # -- internals: routing ----------------------------------------------

    def _route(self, model_request: Any, result: InferenceResult, *,
               task_id: str, attempt_id: str,
               classification: str) -> Optional[RoutingPlan]:
        started = time.time()
        routing_request = RoutingRequest(
            capability=str(model_request.capability or ""),
            required_capabilities=tuple(
                model_request.effective_capabilities()),
            task_type=str(model_request.task or ""),
            complexity=float(getattr(model_request, "complexity", 1.0) or 1.0),
            context_size=int(getattr(model_request, "min_context_window", 0)
                             or 0),
            latency_target_ms=getattr(model_request, "max_latency_ms", None),
            max_output_tokens=getattr(model_request, "max_output_tokens", None),
            timeout=float(getattr(model_request, "timeout", 0.0) or 0.0)
            or None,
            model=str(getattr(model_request, "model", "") or ""),
            backend=str(getattr(model_request, "backend", "") or ""),
            classification=classification or str(
                getattr(model_request, "classification", "") or ""),
            prompt=str(model_request.prompt or ""),
            context_text=str(model_request.context or ""),
            network_policy=str(getattr(model_request, "network_policy", "")
                               or ""),
            cost_budget_usd=getattr(model_request, "cost_budget_usd", None),
            max_cost_per_token=getattr(model_request, "max_cost_per_token",
                                       None),
            prefer_free=getattr(model_request, "prefer_free", None),
            prefer_local=getattr(model_request, "prefer_local", None),
            hardware_profile=str(getattr(model_request, "hardware_profile", "")
                                 or ""),
            require_verified=bool(
                getattr(model_request, "require_verified", True)),
            allow_deterministic=bool(
                getattr(model_request, "allow_deterministic", True)),
            task_id=task_id or result.task_id,
            attempt_id=attempt_id or result.attempt_id,
            trace_id=str(getattr(model_request, "trace_id", "") or ""),
        )
        plan = self.routing.route(routing_request)
        result.routing = {
            "selected_model": plan.selected_model_id,
            "selected_backend": plan.selected_backend,
            "reason": plan.reason,
            "reasons": list(plan.reasons),
            "state": plan.state,
            "score": plan.score,
            "factors": plan.factors,
            "fallback_path": list(plan.fallback_path),
            "considered": len(plan.considered),
            "rejected": [dict(item) for item in plan.rejected],
            "bounds": dict(plan.bounds),
        }
        result.policy_result = plan.policy_result.to_dict()
        result.resource_result = plan.resource_result.to_dict()
        result.metadata["routing_latency_ms"] = round(
            (time.time() - started) * 1000.0, 3)
        if plan.selected_model is not None:
            result.model_id = plan.selected_model_id
            result.backend_id = plan.selected_backend
            result.availability_state = plan.selected_model.availability_state
            result.verification_state = plan.selected_model.verification_state
            result.neural = bool(plan.selected_model.metadata.get("neural",
                                                                  True))
        result.observe("routing", state=plan.state, detail=plan.reason[:400],
                       error_code=plan.error_code, started_at=started,
                       selected_model=plan.selected_model_id,
                       considered=len(plan.considered))
        if plan.state in (RoutingState.NEEDS_MODEL,
                          RoutingState.POLICY_DENIED,
                          RoutingState.RESOURCE_DENIED,
                          RoutingState.UNVERIFIED) and plan.selected_model is None:
            result.success = False
            result.state = plan.state
            result.error = plan.reason
            result.error_code = plan.error_code or "NO_MODEL"
            result.fallback = {"used": False,
                               "ladder": plan.ladder.to_dict()
                               if plan.ladder else None}
            self._finish(result, self._clock())
            return None
        return plan

    # -- internals: one ladder step ---------------------------------------

    def _run_step(self, model_request: Any, result: InferenceResult,
                  step: FallbackStep, plan: RoutingPlan, token: Any,
                  guard: Any) -> Dict[str, Any]:
        from forge.runtime.model_runtime import RuntimeRequest

        started = self._clock()
        identity = self.catalog.find(step.model_id)
        if identity is None:
            return {"success": False, "state": TerminalState.NEEDS_MODEL.value,
                    "error": "model %s is not registered" % step.model_id,
                    "error_code": "NOT_FOUND",
                    "latency_ms": (self._clock() - started) * 1000.0}
        backend = self.catalog.backends.get(step.backend_id or
                                            identity.backend_id)
        result.model_id = identity.model_id
        result.backend_id = backend.backend_id
        result.provider = identity.provider_id or identity.backend_id
        result.availability_state = identity.availability_state
        result.verification_state = identity.verification_state

        # Optional real verification on first use.
        if self.auto_verify and not identity.verified:
            verification = self.catalog.verify(identity.model_id)
            result.verification = verification.to_dict()
            if not verification.verified:
                return {"success": False,
                        "state": TerminalState.UNVERIFIED.value,
                        "error": verification.detail or verification.error,
                        "error_code": verification.error_code or "UNVERIFIED",
                        "latency_ms": (self._clock() - started) * 1000.0}

        # Residency: bounded, refcounted, single-flight.
        try:
            handle = self.catalog.residency(identity.model_id, timeout=None)
        except CatalogError as exc:
            state = (TerminalState.RESOURCE_DENIED.value
                     if exc.code in ("RESOURCE_DENIED", "RESIDENCY_FULL",
                                     "RESIDENCY_BUDGET", "RAM", "CONCURRENCY",
                                     "LOAD_FAILED")
                     else TerminalState.POLICY_DENIED.value
                     if exc.code in ("NETWORK_POLICY",)
                     else TerminalState.FAILED.value)
            result.observe("residency", state=state, detail=exc.message,
                           error_code=exc.code)
            return {"success": False, "state": state, "error": exc.message,
                    "error_code": exc.code,
                    "latency_ms": (self._clock() - started) * 1000.0}

        with handle as entry:
            # Context budgeting against the model's real context limit.
            context_plan = self._plan_context(model_request, identity, plan)
            result.context = context_plan.to_dict()
            if not context_plan.feasible:
                result.observe("context", state=TerminalState.FAILED.value,
                               detail=context_plan.reason,
                               error_code="CONTEXT_OVER_BUDGET")
                return {"success": False,
                        "state": TerminalState.FAILED.value,
                        "error": context_plan.reason,
                        "error_code": "CONTEXT_OVER_BUDGET",
                        "latency_ms": (self._clock() - started) * 1000.0}

            bounds = plan.bounds or {}
            runtime_request = RuntimeRequest(
                prompt=str(model_request.prompt or ""),
                model=identity.model_id,
                backend=backend.backend_name
                if hasattr(backend, "backend_name") else backend.backend_id,
                context=context_plan.render(),
                task=str(model_request.task or ""),
                instructions=str(model_request.constraints_text()
                                 if hasattr(model_request, "constraints_text")
                                 else ""),
                max_output_tokens=int(bounds.get("max_output_tokens")
                                      or getattr(model_request,
                                                 "max_output_tokens", None)
                                      or 2048),
                temperature=getattr(model_request, "temperature", None),
                timeout=float(bounds.get("timeout") or 60.0),
                request_id=result.request_id,
                trace_id=str(getattr(model_request, "trace_id", "") or ""),
                metadata={"task_id": result.task_id,
                          "attempt_id": result.attempt_id,
                          "generation_id": result.generation_id,
                          "tier": step.tier},
            )
            result.observe("generate", state="running", started_at=time.time(),
                           detail="backend=%s model=%s"
                                  % (backend.backend_id, identity.model_id),
                           timeout=runtime_request.timeout,
                           max_output_tokens=runtime_request.max_output_tokens)
            try:
                response = backend.generate(runtime_request, token=token)
            except BackendError as exc:
                state = classify_error(exc.message, code=exc.code)
                result.observe("generate", state=state, detail=exc.message,
                               error_code=exc.code)
                return {"success": False, "state": state,
                        "error": exc.message, "error_code": exc.code,
                        "latency_ms": (self._clock() - started) * 1000.0}
            except ModelSpoofingError as exc:
                result.observe("generate", state=TerminalState.FAILED.value,
                               detail=str(exc), error_code=exc.code)
                return {"success": False,
                        "state": TerminalState.FAILED.value,
                        "error": str(exc), "error_code": exc.code,
                        "latency_ms": (self._clock() - started) * 1000.0}
            except Exception as exc:
                text = _redact(str(exc))
                state = classify_error(text, code=type(exc).__name__.upper())
                result.observe("generate", state=state, detail=text,
                               error_code=type(exc).__name__)
                return {"success": False, "state": state, "error": text,
                        "error_code": "BACKEND_ERROR",
                        "latency_ms": (self._clock() - started) * 1000.0}

        # The fence decides whether this result may be published at all.
        stale = self._fence_reason(guard, result)
        if stale:
            return self._stale(result, started, stale, drop_text=True)

        if not getattr(response, "success", False):
            error = _redact(str(getattr(response, "error", "") or ""))
            kind = str(getattr(response, "error_kind", "") or "")
            state = classify_error(error, code=kind.upper(),
                                   timed_out=bool(getattr(response,
                                                          "timed_out", False)),
                                   cancelled=bool(getattr(response,
                                                          "cancelled", False)))
            result.observe("generate", state=state, detail=error,
                           error_code=kind or "BACKEND_ERROR")
            self.catalog.record_outcome(
                identity.model_id, success=False,
                latency_ms=float(getattr(response, "latency_ms", 0.0) or 0.0),
                error=error, error_code=kind,
                capability=str(model_request.capability or ""))
            return {"success": False, "state": state, "error": error,
                    "error_code": kind or "BACKEND_ERROR",
                    "latency_ms": (self._clock() - started) * 1000.0}

        # Identity check: the backend must have answered with *this* model.
        reported_model = str(getattr(response, "model", "") or "")
        try:
            identity.assert_same_model(
                reported_model=reported_model,
                reported_backend=str(getattr(response, "backend", "") or ""))
        except ModelSpoofingError as exc:
            result.observe("identity", state=TerminalState.FAILED.value,
                           detail=str(exc), error_code=exc.code)
            return {"success": False, "state": TerminalState.FAILED.value,
                    "error": str(exc), "error_code": exc.code,
                    "latency_ms": (self._clock() - started) * 1000.0}

        text = str(getattr(response, "text", "") or "")
        truncated = False
        if len(text) > self.max_output_chars:
            text = text[:self.max_output_chars]
            truncated = True
        result.text = text
        result.truncated = truncated
        result.success = True
        result.state = TerminalState.SUCCEEDED.value
        result.done = True
        result.finish_reason = str(getattr(response, "finish_reason", "")
                                   or "stop")
        result.input_tokens = int(getattr(response, "input_tokens", 0) or 0)
        result.output_tokens = int(getattr(response, "output_tokens", 0) or 0)
        result.provider = identity.provider_id or identity.backend_id
        result.model_id = identity.model_id
        result.backend_id = str(getattr(response, "backend", "")
                                or backend.backend_id)
        result.output_scan = scan_model_output(text)
        result.metadata["reported_model"] = reported_model[:200]
        result.metadata["residency"] = entry.to_dict()
        latency = float(getattr(response, "latency_ms", 0.0) or 0.0) \
            or (self._clock() - started) * 1000.0
        result.observe("generate", state=TerminalState.SUCCEEDED.value,
                       detail="completed", started_at=time.time(),
                       output_tokens=result.output_tokens,
                       truncated=truncated,
                       output_flags=list(result.output_scan.get("flags") or ()))
        self.catalog.record_outcome(
            identity.model_id, success=True, latency_ms=latency,
            capability=str(model_request.capability or ""))
        return {"success": True, "state": TerminalState.SUCCEEDED.value,
                "error": "", "error_code": "", "latency_ms": latency}

    def _run_deterministic(self, model_request: Any, result: InferenceResult,
                           step: FallbackStep) -> Dict[str, Any]:
        """The non-neural rung: honest, deterministic, never fabricated."""
        started = self._clock()
        provider = self.deterministic_provider
        result.neural = False
        result.model_id = step.model_id
        result.backend_id = step.backend_id
        result.provider = "local"
        result.availability_state = AvailabilityState.READY.value
        result.verification_state = VerificationState.UNVERIFIED.value
        if provider is None:
            message = ("no deterministic strategy is available; refusing to "
                       "fabricate model output")
            result.observe("deterministic",
                           state=TerminalState.NEEDS_MODEL.value,
                           detail=message, error_code="NEEDS_MODEL")
            return {"success": False, "state": TerminalState.NEEDS_MODEL.value,
                    "error": message, "error_code": "NEEDS_MODEL",
                    "latency_ms": (self._clock() - started) * 1000.0}
        try:
            produced = provider.generate(
                str(model_request.prompt or ""),
                context=str(model_request.context or ""),
                task=str(model_request.task or ""))
        except TypeError:
            try:
                produced = provider.generate(str(model_request.prompt or ""))
            except Exception as exc:
                return {"success": False,
                        "state": TerminalState.FAILED.value,
                        "error": _redact(str(exc)),
                        "error_code": "DETERMINISTIC_FAILED",
                        "latency_ms": (self._clock() - started) * 1000.0}
        except Exception as exc:
            return {"success": False, "state": TerminalState.FAILED.value,
                    "error": _redact(str(exc)),
                    "error_code": "DETERMINISTIC_FAILED",
                    "latency_ms": (self._clock() - started) * 1000.0}
        text = str(getattr(produced, "text", "") or "")
        result.text = text
        result.success = True
        # A deterministic answer is *complete* but it is not a model answer.
        result.state = TerminalState.SUCCEEDED.value
        result.done = True
        result.finish_reason = "deterministic"
        result.output_scan = scan_model_output(text)
        result.metadata["deterministic"] = True
        result.metadata["neural"] = False
        result.observe("deterministic", state=TerminalState.SUCCEEDED.value,
                       detail=("deterministic non-neural strategy answered; "
                               "no model produced this text"),
                       started_at=time.time())
        self._count("deterministic")
        return {"success": True, "state": TerminalState.SUCCEEDED.value,
                "error": "", "error_code": "",
                "latency_ms": (self._clock() - started) * 1000.0}

    # -- internals: streaming ---------------------------------------------

    def _produce_stream(self, model_request: Any, result: InferenceResult,
                        stream: BoundedStream, guard: Any,
                        classification: str) -> None:
        from forge.runtime.model_runtime import RuntimeRequest

        started = self._clock()
        token = self._register_token(result.request_id)
        try:
            self._auto_prepare(model_request, result)
            plan = self._route(model_request, result, task_id=result.task_id,
                               attempt_id=result.attempt_id,
                               classification=classification)
            if plan is None or plan.selected_model is None:
                stream.model_id = result.model_id
                stream.backend_id = result.backend_id
                stream.fail(result.error or "no model could serve",
                            code=result.error_code or "NO_MODEL")
                self._finish(result, started)
                return
            stale = self._fence_reason(guard, result)
            if stale:
                self._stale(result, started, stale)
                stream.fail("execution fence is no longer valid",
                            code="STALE", finish_reason="cancelled")
                return

            identity = plan.selected_model
            step = _step_for(plan, identity)
            if not step.neural or identity.model_id == \
                    self.deterministic_identity.model_id:
                # The ladder selected the non-neural rung. It has no backend
                # and must never be streamed as if a model produced it.
                self._stream_deterministic(model_request, result, step, stream,
                                           guard, started)
                return
            backend = self.catalog.backends.get(step.backend_id
                                                or identity.backend_id)
            result.model_id = identity.model_id
            result.backend_id = backend.backend_id
            result.provider = identity.provider_id or identity.backend_id
            stream.model_id = identity.model_id
            stream.backend_id = backend.backend_id

            try:
                handle = self.catalog.residency(identity.model_id)
            except CatalogError as exc:
                state = classify_error(exc.message, code=exc.code)
                result.state = state
                result.error = exc.message
                result.error_code = exc.code
                stream.fail(exc.message, code=exc.code)
                self._finish(result, started)
                return

            with handle:
                context_plan = self._plan_context(model_request, identity,
                                                  plan)
                result.context = context_plan.to_dict()
                if not context_plan.feasible:
                    result.state = TerminalState.FAILED.value
                    result.error = context_plan.reason
                    result.error_code = "CONTEXT_OVER_BUDGET"
                    stream.fail(context_plan.reason,
                                code="CONTEXT_OVER_BUDGET")
                    self._finish(result, started)
                    return
                bounds = plan.bounds or {}
                runtime_request = RuntimeRequest(
                    prompt=str(model_request.prompt or ""),
                    model=identity.model_id,
                    backend=getattr(backend, "backend_name",
                                    backend.backend_id),
                    context=context_plan.render(),
                    task=str(model_request.task or ""),
                    instructions=str(model_request.constraints_text()
                                     if hasattr(model_request,
                                                "constraints_text") else ""),
                    max_output_tokens=int(bounds.get("max_output_tokens")
                                          or 2048),
                    temperature=getattr(model_request, "temperature", None),
                    timeout=float(bounds.get("timeout") or 60.0),
                    request_id=result.request_id,
                    trace_id=str(getattr(model_request, "trace_id", "") or ""),
                    metadata={"task_id": result.task_id,
                              "attempt_id": result.attempt_id,
                              "generation_id": result.generation_id,
                              "stream": True},
                )
                first: List[float] = []
                chars = 0
                try:
                    for chunk in backend.stream(runtime_request, token=token):
                        delta = str(getattr(chunk, "text", "") or "")
                        if not delta:
                            if getattr(chunk, "done", False):
                                break
                            continue
                        if not first:
                            first.append(self._clock())
                            result.time_to_first_token_ms = round(
                                (first[0] - started) * 1000.0, 3)
                        if chars + len(delta) > self.max_output_chars:
                            delta = delta[:max(0, self.max_output_chars - chars)]
                            result.truncated = True
                        if not delta:
                            break
                        chars += len(delta)
                        if not stream.publish(
                                delta,
                                output_tokens=int(getattr(chunk,
                                                          "output_tokens", 0)
                                                  or 0)):
                            break
                        stale = self._fence_reason(guard, result)
                        if stale:
                            self._stale(result, started, stale, drop_text=True)
                            stream.fail("execution fence is no longer valid",
                                        code="STALE",
                                        finish_reason="cancelled")
                            return
                except BackendError as exc:
                    result.state = classify_error(exc.message, code=exc.code)
                    result.error = exc.message
                    result.error_code = _honest_error_code(result.state,
                                                           exc.code)
                    stream.fail(exc.message, code=result.error_code)
                    self.catalog.record_outcome(identity.model_id,
                                                success=False, error=exc.message,
                                                error_code=result.error_code)
                    self._finish(result, started)
                    return
                except Exception as exc:
                    text = _redact(str(exc))
                    result.state = classify_error(text,
                                                  code=type(exc).__name__)
                    result.error = text
                    result.error_code = _honest_error_code(
                        result.state, "BACKEND_ERROR")
                    stream.fail(text, code=result.error_code)
                    self._finish(result, started)
                    return

            stale = self._fence_reason(guard, result)
            if stale:
                self._stale(result, started, stale, drop_text=True)
                stream.fail("execution fence is no longer valid", code="STALE",
                            finish_reason="cancelled")
                return
            if stream.cancelled or stream.state == "cancelled":
                #: A cancellation is reported as a cancellation, whatever the
                #: producer was doing when the signal landed. Calling it
                #: STREAM_INCOMPLETE would hide who ended the attempt, and
                #: keeping the partial text would publish output from an
                #: attempt the caller already withdrew.
                snapshot = stream.snapshot()
                result.text = ""
                result.output_tokens = 0
                result.input_tokens = len(str(model_request.prompt or ""))
                result.success = False
                result.done = True
                result.state = TerminalState.CANCELLED.value
                result.error = str(snapshot.get("error")
                                   or "stream cancelled")
                result.error_code = "CANCELLED"
                result.finish_reason = "cancelled"
                result.metadata["stream"] = snapshot
                self.catalog.record_outcome(
                    identity.model_id, success=False,
                    latency_ms=(self._clock() - started) * 1000.0,
                    error=result.error, error_code=result.error_code)
                self._finish(result, started)
                return
            text = stream.text()
            result.text = text
            result.output_tokens = len(text)
            result.input_tokens = len(str(model_request.prompt or ""))
            result.output_scan = scan_model_output(text)
            finish = "length" if result.truncated else "stop"
            stream.finish(finish_reason=finish, output_tokens=len(text))
            result.success = stream.complete
            result.done = True
            result.finish_reason = finish if stream.complete else stream.state
            result.state = (TerminalState.SUCCEEDED.value if stream.complete
                            else TerminalState.FAILED.value)
            if not stream.complete:
                result.error = ("stream ended in state %r" % stream.state)
                result.error_code = "STREAM_INCOMPLETE"
            result.metadata["stream"] = stream.snapshot()
            self.catalog.record_outcome(identity.model_id,
                                        success=result.success,
                                        latency_ms=(self._clock() - started)
                                        * 1000.0,
                                        error=result.error,
                                        error_code=result.error_code)
            self._finish(result, started)
        except BaseException as exc:  # pragma: no cover - safety net
            # A producer that dies must still terminate its stream: a
            # consumer blocked on ``events()`` would otherwise wait forever.
            text = _redact(str(exc))
            result.success = False
            result.error = result.error or text
            result.error_code = result.error_code or "STREAM_FAILED"
            result.state = result.state or TerminalState.FAILED.value
            if not stream.done:
                stream.fail(result.error, code=result.error_code)
            self._finish(result, started)
            raise
        finally:
            if not stream.done:
                # Whatever happened, the stream reaches a terminal state.
                stream.fail(result.error or
                            "stream producer ended without completing",
                            code=result.error_code or "STREAM_INCOMPLETE")
            result.done = True
            self._unregister_token(result.request_id)
            with self._lock:
                self._streams.pop(result.request_id, None)

    def _stream_deterministic(self, model_request: Any,
                              result: InferenceResult, step: FallbackStep,
                              stream: BoundedStream, guard: Any,
                              started: float) -> None:
        """Stream the non-neural rung honestly.

        There is no backend here and no model produced this text: the deltas
        are the deterministic strategy's own output, the result stays
        ``neural=False``, and the finish reason says so. Fabricating a
        model-shaped answer is the one thing this path must never do.
        """
        outcome = self._run_deterministic(model_request, result, step)
        stream.model_id = result.model_id
        stream.backend_id = result.backend_id
        step.outcome = str(outcome.get("state") or TerminalState.FAILED.value)
        step.error = str(outcome.get("error") or "")
        step.error_code = str(outcome.get("error_code") or "")
        step.latency_ms = float(outcome.get("latency_ms") or 0.0)
        if not outcome.get("success"):
            stream.fail(result.error or step.error or
                        "no deterministic strategy is available",
                        code=result.error_code or step.error_code
                        or "NEEDS_MODEL")
            self._finish(result, started)
            return

        stale = self._fence_reason(guard, result)
        if stale:
            self._stale(result, started, stale, drop_text=True)
            stream.fail("execution fence is no longer valid", code="STALE",
                        finish_reason="cancelled")
            return

        text = str(result.text or "")
        result.text = ""
        result.input_tokens = len(str(model_request.prompt or ""))
        chunk = _DETERMINISTIC_CHUNK_CHARS
        published = 0
        first = 0.0
        while published < len(text):
            delta = text[published:published + chunk]
            published += len(delta)
            if not first:
                first = self._clock()
                result.time_to_first_token_ms = round((first - started)
                                                      * 1000.0, 3)
            if not stream.publish(delta):
                break
            stale = self._fence_reason(guard, result)
            if stale:
                self._stale(result, started, stale, drop_text=True)
                stream.fail("execution fence is no longer valid",
                            code="STALE", finish_reason="cancelled")
                return
        streamed = stream.text()
        result.text = streamed
        result.output_tokens = len(streamed)
        result.output_scan = scan_model_output(streamed)
        stream.finish(finish_reason="deterministic",
                      output_tokens=len(streamed))
        result.success = bool(stream.complete)
        result.done = True
        result.finish_reason = ("deterministic" if stream.complete
                                else str(stream.state))
        result.state = (TerminalState.SUCCEEDED.value if stream.complete
                        else TerminalState.FAILED.value)
        if not stream.complete:
            result.error = ("stream ended in state %r" % stream.state)
            result.error_code = "STREAM_INCOMPLETE"
        result.neural = False
        result.metadata["stream"] = stream.snapshot()
        result.metadata["neural"] = False
        result.metadata["deterministic"] = True
        self.catalog.record_outcome(result.model_id, success=result.success,
                                    latency_ms=(self._clock() - started)
                                    * 1000.0, error=result.error,
                                    error_code=result.error_code)
        self._finish(result, started)

    # -- internals: context -----------------------------------------------

    def _plan_context(self, model_request: Any, identity: ModelIdentity,
                      plan: RoutingPlan) -> ContextPlan:
        limit = int(identity.context_limit or 0)
        bounds = plan.bounds or {}
        sections = []
        prompt = str(getattr(model_request, "prompt", "") or "")
        task = str(getattr(model_request, "task", "") or "")
        context = str(getattr(model_request, "context", "") or "")
        instructions = ""
        if hasattr(model_request, "constraints_text"):
            try:
                instructions = str(model_request.constraints_text() or "")
            except Exception:
                instructions = ""
        pack = getattr(model_request, "context_pack", None)
        research = tuple(getattr(model_request, "research_evidence", ()) or ())
        memory = tuple(getattr(model_request, "memory_records", ()) or ())
        return self.planner.plan_for_request(
            prompt=prompt, task=task, instructions=instructions,
            context=context, pack=pack, research=research, memory=memory,
            context_limit=limit,
            max_output_tokens=int(bounds.get("max_output_tokens") or 0) or None)

    # -- internals: fencing, tokens, bookkeeping ---------------------------

    def _new_result(self, model_request: Any, *, task_id: str,
                    attempt_id: str, fence: Any,
                    generation_id: str) -> InferenceResult:
        request_id = "inf-" + uuid4().hex[:16]
        generation = generation_id
        if fence is not None and not generation:
            generation = str(getattr(fence, "attempt_id", "") or "")
        result = InferenceResult(
            request_id=request_id,
            task_id=task_id or str(getattr(model_request, "task_id", "") or ""),
            attempt_id=attempt_id or str(
                getattr(model_request, "attempt_id", "") or ""),
            generation_id=generation or request_id,
            started_at=time.time(),
            metadata={
                "trace_id": str(getattr(model_request, "trace_id", "") or ""),
                "capability": str(getattr(model_request, "capability", "")
                                  or ""),
            })
        with self._lock:
            self._results[request_id] = result
            if len(self._results) > 256:
                for key in sorted(self._results,
                                  key=lambda item: self._results[item].started_at
                                  )[:64]:
                    self._results.pop(key, None)
        return result

    def _register_token(self, request_id: str) -> Any:
        from forge.runtime.model_runtime import CancellationToken

        token = CancellationToken()
        with self._lock:
            self._tokens[request_id] = token
        return token

    def _unregister_token(self, request_id: str) -> None:
        with self._lock:
            self._tokens.pop(request_id, None)

    def _fence_reason(self, guard: Any, result: InferenceResult) -> str:
        if guard is None:
            return ""
        try:
            reason = guard()
        except Exception as exc:
            reason = "commit guard errored: %s" % (exc,)
        if reason:
            result.metadata["fenced"] = True
            result.metadata["fence_reason"] = str(reason)[:300]
        return str(reason or "")

    def _stale(self, result: InferenceResult, started: float, reason: str,
               *, drop_text: bool = False) -> Any:
        """Discard a result whose attempt is no longer authorized."""
        if drop_text:
            result.text = ""
        result.success = False
        result.state = TerminalState.STALE.value
        result.error = ("result discarded: %s" % reason)[:400]
        result.error_code = "STALE"
        result.done = False
        result.observe("fence", state=TerminalState.STALE.value, detail=reason,
                       error_code="STALE")
        self._count("stale")
        return self._finish(result, started)

    def _label_result(self, result: InferenceResult) -> InferenceResult:
        """§20/§30 — a Session-11 result says it came from the Session-11 path.

        Consumers that hold this fabric directly (an agent, the CLI, a
        benchmark) never travel the ``ModelFabric`` seam, so the seam cannot
        label their responses; without this the honest fallback in
        ``provenance_from_response`` would have to call real Session-11 work
        "legacy", which is a false label in the one place labels matter.

        ``setdefault`` only: whatever the run already recorded (a routing
        reason, a fallback rung, an explicit stamp from the seam) wins over a
        generic value. Bounded metadata, identifiers and verdicts — never
        prompt text, never a completion.
        """
        from forge.models.inference_path import PATH_SESSION11

        metadata = result.metadata
        metadata.setdefault("inference_path", PATH_SESSION11)
        metadata.setdefault("model", result.model_id)
        metadata.setdefault("provider", result.provider)
        metadata.setdefault("backend_id", result.backend_id)
        metadata.setdefault("neural", bool(result.neural))
        metadata.setdefault("deterministic", not bool(result.neural))
        metadata.setdefault("state", result.state)
        metadata.setdefault("error_code", result.error_code)
        metadata.setdefault("verification_state", result.verification_state)
        metadata.setdefault("availability_state", result.availability_state)
        metadata.setdefault("truncated", bool(result.truncated))
        metadata.setdefault("streamed", bool(result.streamed))
        for key, value in (("request_id", result.request_id),
                           ("generation_id", result.generation_id),
                           ("task_id", result.task_id),
                           ("attempt_id", result.attempt_id)):
            if value:
                metadata.setdefault(key, value)
        routing = result.routing or {}
        if routing.get("reason"):
            metadata.setdefault("routing_reason",
                                str(routing.get("reason"))[:200])
        fallback = result.fallback or {}
        metadata.setdefault("fallback_used",
                            bool(fallback.get("fallback_used")))
        if fallback.get("rung"):
            metadata.setdefault("fallback_rung",
                                str(fallback.get("rung"))[:64])
        return result

    def _finish(self, result: InferenceResult, started: float) -> Any:
        result.finished_at = time.time()
        result.latency_ms = (self._clock() - started) * 1000.0
        if not result.success and not result.finish_reason:
            #: Every finished attempt says why it ended, including the ones
            #: that never reached a backend response.
            derived = {
                TerminalState.CANCELLED.value: "cancelled",
                TerminalState.STALE.value: "stale",
                TerminalState.TIMEOUT.value: "timeout",
            }.get(result.state, "error")
            result.finish_reason = derived
        #: ``neural`` is a claim about *provenance*: it may only be true when a
        #: real model backend produced this text. A refusal, a cancellation, a
        #: stale fence, an empty completion or the deterministic rung never
        #: claims a model -- reporting one would be exactly the kind of
        #: invented success this fabric exists to prevent.
        if (not result.success or not result.text
                or result.backend_id in ("", "deterministic")):
            result.neural = False
        self._label_result(result)
        record = result.to_dict()
        with self._lock:
            self._history.append(record)
            if len(self._history) > 200:
                self._history = self._history[-200:]
            key = "success" if result.success else result.state
            self._counts[key] = int(self._counts.get(key, 0)) + 1
            self._counts["requests"] = int(self._counts.get("requests", 0)) + 1
        if self.telemetry is not None:
            try:
                self.telemetry.record(
                    "inference",
                    request_id=result.request_id, task_id=result.task_id,
                    attempt_id=result.attempt_id,
                    generation_id=result.generation_id,
                    model_id=result.model_id, backend_id=result.backend_id,
                    state=result.state, success=result.success,
                    neural=result.neural, error_code=result.error_code,
                    latency_ms=result.latency_ms,
                    time_to_first_token_ms=result.time_to_first_token_ms,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    text_chars=len(result.text or ""),
                    truncated=result.truncated,
                    streamed=result.streamed,
                    fallback_used=bool(result.fallback.get("used")),
                    policy_allowed=result.policy_result.get("allowed"),
                    resource_allowed=result.resource_result.get("allowed"),
                    classification=result.policy_result.get(
                        "data_classification", ""),
                    profile=result.resource_result.get("profile", ""),
                    verification_state=result.verification_state,
                    availability_state=result.availability_state,
                    output_flags=list(result.output_scan.get("flags") or ()),
                )
            except Exception:
                pass
        return result

    def _count(self, key: str) -> None:
        with self._lock:
            self._counts[key] = int(self._counts.get(key, 0)) + 1

    # -- self-improvement evidence (§24) -----------------------------------

    def evidence(self, *, limit: int = 200) -> Dict[str, Any]:
        """Routing evidence for self-improvement. Proposals only.

        Nothing here may change a permission, a security rule, a provider
        authorization, or a credential policy: the payload is diagnostic and
        the improvement engine treats it as a finding, not as authority.
        """
        with self._lock:
            history = [dict(item) for item in self._history[-max(1, limit):]]
            counts = dict(self._counts)
        total = len(history)
        failures = [item for item in history if not item.get("success")]
        timeouts = [item for item in history
                    if item.get("state") == TerminalState.TIMEOUT.value]
        resource = [item for item in history
                    if item.get("state") == TerminalState.RESOURCE_DENIED.value]
        policy = [item for item in history
                  if item.get("state") == TerminalState.POLICY_DENIED.value]
        fallbacks = [item for item in history
                     if item.get("fallback", {}).get("used")]
        stale = [item for item in history
                 if item.get("state") == TerminalState.STALE.value]
        latencies = sorted(float(item.get("latency_ms") or 0.0)
                           for item in history if item.get("success"))
        per_model: Dict[str, Dict[str, Any]] = {}
        for item in history:
            key = item.get("model_id") or "-"
            bucket = per_model.setdefault(
                key, {"requests": 0, "failures": 0, "timeouts": 0,
                      "latency_ms": []})
            bucket["requests"] += 1
            if not item.get("success"):
                bucket["failures"] += 1
            if item.get("state") == TerminalState.TIMEOUT.value:
                bucket["timeouts"] += 1
            if item.get("success"):
                bucket["latency_ms"].append(float(item.get("latency_ms") or 0))
        for bucket in per_model.values():
            values = sorted(bucket.pop("latency_ms"))
            bucket["latency_p50_ms"] = _percentile(values, 50)
            bucket["latency_p95_ms"] = _percentile(values, 95)
        return {
            "requests": total,
            "counts": counts,
            "success_rate": (round((total - len(failures)) / total, 4)
                             if total else None),
            "failure_rate": round(len(failures) / total, 4) if total else None,
            "timeouts": len(timeouts),
            "resource_denials": len(resource),
            "policy_denials": len(policy),
            "fallbacks": len(fallbacks),
            "stale_results": len(stale),
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
            "per_model": per_model,
            "error_codes": _tally(item.get("error_code") for item in failures),
            "proposals": _proposals(total, failures, timeouts, resource,
                                    policy, fallbacks, stale, per_model),
            "authority": ("diagnostic only: routing proposals may not change "
                          "permissions, security rules, provider "
                          "authorization, or credential policy"),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values or ())
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = min(len(ordered) - 1,
                max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def _tally(values: Any) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _proposals(total: int, failures: List[Dict[str, Any]],
               timeouts: List[Dict[str, Any]],
               resource: List[Dict[str, Any]],
               policy: List[Dict[str, Any]],
               fallbacks: List[Dict[str, Any]],
               stale: List[Dict[str, Any]],
               per_model: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Routing change proposals (never permission changes)."""
    proposals: List[Dict[str, Any]] = []
    if not total:
        return proposals
    if len(timeouts) / float(total) > 0.2:
        proposals.append({
            "kind": "timeout_rate",
            "severity": "high",
            "evidence": "%d/%d requests timed out" % (len(timeouts), total),
            "proposal": "raise the routed timeout bound or prefer a lower "
                        "latency model for this capability",
        })
    if len(resource) / float(total) > 0.2:
        proposals.append({
            "kind": "resource_denial_rate",
            "severity": "high",
            "evidence": "%d/%d requests were resource-denied"
                        % (len(resource), total),
            "proposal": "route this capability to the server profile or "
                        "reduce the residency footprint",
        })
    if len(policy) / float(total) > 0.2:
        proposals.append({
            "kind": "policy_denial_rate",
            "severity": "medium",
            "evidence": "%d/%d requests were policy-denied"
                        % (len(policy), total),
            "proposal": "register a local verified model for the affected "
                        "classification, or request an explicit policy "
                        "allowance through the canonical approval path",
        })
    if len(fallbacks) / float(total) > 0.3:
        proposals.append({
            "kind": "fallback_frequency",
            "severity": "medium",
            "evidence": "%d/%d requests used a fallback rung"
                        % (len(fallbacks), total),
            "proposal": "verify and promote a preferred model for the "
                        "affected capability",
        })
    if stale:
        proposals.append({
            "kind": "stale_results",
            "severity": "medium",
            "evidence": "%d results were discarded as stale" % len(stale),
            "proposal": "check retry/timeout configuration of the scheduler "
                        "fence for these tasks",
        })
    for model_id, bucket in sorted(per_model.items()):
        requests = int(bucket.get("requests") or 0)
        failures_count = int(bucket.get("failures") or 0)
        if requests >= 3 and failures_count / float(requests) > 0.5:
            proposals.append({
                "kind": "model_failure_rate",
                "severity": "high",
                "evidence": "model %s failed %d/%d requests"
                            % (model_id, failures_count, requests),
                "proposal": "re-verify %s and demote it in the routing order "
                            "until it passes" % model_id,
            })
        p95 = bucket.get("latency_p95_ms")
        if isinstance(p95, (int, float)) and p95 > 30000:
            proposals.append({
                "kind": "high_latency",
                "severity": "low",
                "evidence": "model %s p95 latency is %.0fms" % (model_id, p95),
                "proposal": "prefer a lower-latency model when the task has a "
                            "latency target",
            })
    return proposals


def _step_for(plan: RoutingPlan, identity: ModelIdentity) -> FallbackStep:
    ladder = plan.ladder
    if ladder is not None:
        for step in ladder.steps:
            if step.model_id == identity.model_id:
                return step
    return FallbackStep(order=1, tier="preferred_verified",
                        model_id=identity.model_id,
                        backend_id=identity.backend_id)


def _guard_from(fence: Any, registry: Any) -> Optional[Callable[[], str]]:
    """Build the canonical commit guard for a fence (Session 10 semantics).

    Without the registry a guard can only see the local fence object, which a
    superseding attempt does not mutate; ``FenceRegistry`` is the authority on
    which generation may publish, so callers that have one must pass it.
    """
    try:
        from forge.core.fencing import commit_guard
        return commit_guard(fence, registry)
    except Exception:
        def _fallback_guard() -> str:
            if not fence.authorized():
                return ("attempt %s is fenced (%s)"
                        % (getattr(fence, "attempt_id", "?"),
                           getattr(fence, "reason", "") or
                           getattr(fence, "state", "")))
            return ""
        return _fallback_guard


def _coerce_request(request: Any, model_request_type: Any) -> Any:
    if isinstance(request, model_request_type):
        return request
    if isinstance(request, str):
        return model_request_type(prompt=request, capability="coding")
    if request is None:
        raise ValueError("a ModelRequest or prompt string is required")
    prompt = getattr(request, "prompt", None)
    if isinstance(prompt, str):
        return request
    raise ValueError(
        "expected a ModelRequest, got %s" % type(request).__name__)


def _make_deterministic_identity() -> ModelIdentity:
    """Identity of the non-neural rung (explicitly not a model)."""
    identity = ModelIdentity(
        model_id="deterministic:local-fallback",
        provider_id="local", backend_id="deterministic",
        model_family="deterministic-strategy", model_version="1",
        context_limit=4096, max_output_limit=2048, capabilities=(),
        local=True, free=True, quality=0.0,
        availability_state=AvailabilityState.READY.value,
        verification_state=VerificationState.UNVERIFIED.value,
        metadata={"neural": False, "deterministic": True,
                  "description": ("Deterministic non-neural fallback. It "
                                  "refuses to synthesize model output and is "
                                  "never presented as a model answer.")})
    return identity


def _redact(text: str) -> str:
    try:
        from forge.runtime.model_runtime import redact_text
        return redact_text(str(text or ""))[:500]
    except Exception:
        return str(text or "")[:500]


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def build_inference_fabric(*, governor: Any = None, runtime: Any = None,
                           runtime_config: Any = None,
                           policy: Any = None, telemetry: Any = None,
                           data_policy: Any = None,
                           reference_dirs: Sequence[str] = (),
                           allow_network: bool = False,
                           remote_configs: Sequence[Any] = (),
                           fence_registry: Any = None,
                           max_resident_bytes: int = 0, max_slots: int = 2,
                           idle_seconds: float = 0.0,
                           capabilities: Tuple[str, ...] = (),
                           auto_verify: bool = False,
                           deterministic_provider: Any = None) -> InferenceFabric:
    """Build the canonical inference fabric over a Native Model Runtime.

    Nothing is downloaded, nothing is imported from a string, and no network
    call happens here: backends are registered, then discovered on demand.
    """
    from forge.runtime.model_runtime import ModelRuntime, RuntimeConfig

    if runtime is None:
        config = runtime_config if runtime_config is not None \
            else RuntimeConfig.load()
        if allow_network and not config.allow_network:
            config.allow_network = True
        config.validate()
        runtime = ModelRuntime.from_defaults(config, governor=governor)
    elif governor is not None:
        runtime.set_governor(governor)

    if reference_dirs:
        from forge.models.reference_engine import ReferenceLocalBackend

        #: Artifact creation stays explicit: only an operator-set switch (or a
        #: direct CLI command) may write a reference artifact.
        allow_create = str(os.environ.get("FORGE_REFERENCE_CREATE", "")
                           ).strip().lower() in ("1", "true", "yes")
        backend = ReferenceLocalBackend(model_dirs=tuple(reference_dirs),
                                        allow_create=allow_create)
        if not runtime.has_backend(backend.name):
            runtime.register_backend(backend)

    for remote in remote_configs or ():
        from forge.models.remote import RemoteHttpBackend

        remote_backend = RemoteHttpBackend(remote,
                                           allow_network=bool(
                                               allow_network
                                               or runtime.config.allow_network))
        if not runtime.has_backend(remote_backend.name):
            runtime.register_backend(remote_backend)

    if deterministic_provider is None:
        try:
            from forge.models.provider import LocalModelProvider
            deterministic_provider = LocalModelProvider()
        except Exception:
            deterministic_provider = None

    fabric = InferenceFabric.from_runtime(
        runtime, governor=governor, policy=policy, telemetry=telemetry,
        data_policy=data_policy,
        deterministic_provider=deterministic_provider,
        fence_registry=fence_registry,
        max_resident_bytes=max_resident_bytes, max_slots=max_slots,
        idle_seconds=idle_seconds, capabilities=capabilities,
        auto_verify=auto_verify)
    return fabric
