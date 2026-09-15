"""One authoritative model-selection path (Session 11.5).

Session 11 built the real inference fabric. Session 10 built the Model Fabric the
agents actually call. Until now they never met: ``attach_inference`` had no
production caller, so the background engineering loop routed through the legacy
fabric while the Session-11 fabric served only the HTTP endpoints and the CLI.
Two selection paths, no shared authority.

This module removes that split **without** creating a second router. It adds a
typed seam inside the existing :class:`~forge.models.fabric.ModelFabric`:

    Agent → ModelFabric.generate
              → decide_path()            (legacy | session11, recorded)
              → Session11Adapter         (this module)
                 → InferenceFabric → RoutingEngine → ModelCatalog
                    → ModelResidencyCache → Backend → ModelRuntime → model
              → ModelResponse            (legacy vocabulary, rich provenance)

Modes (:class:`InferencePathConfig`):

``legacy``
    Existing behaviour, byte for byte. The Session-11 fabric is never consulted.
``session11``
    Every eligible call goes through the Session-11 fabric. If that fabric cannot
    be obtained, the call **fails** with ``INFERENCE_PATH_UNAVAILABLE`` — it does
    not quietly become a legacy call. Silence about a degraded path is how a
    system ends up claiming neural output it never produced.
``hybrid``
    Eligibility is decided *before* the call, from configuration (capability
    allow-list and an explicit non-migrated caller list), never from whether
    Session 11 happened to fail. A request that is eligible and then fails inside
    the Session-11 fabric is reported as that failure; it is not retried on the
    legacy path.

Honesty rules enforced here:

* every response says which path served it (``metadata["inference_path"]``) and
  why (``metadata["inference_path_reason"]``);
* ``neural`` and ``deterministic`` are carried through unchanged, so a
  deterministic reference answer can never masquerade as a capable model;
* no prompt, context or completion text is copied into metadata;
* verification state is carried through unchanged — ``CONFIGURED`` never becomes
  ``READY`` on the way past this seam.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from uuid import uuid4

__all__ = [
    "PATH_LEGACY",
    "PATH_SESSION11",
    "PATH_HYBRID",
    "VALID_MODES",
    "CODE_PATH_UNAVAILABLE",
    "CODE_PATH_DISABLED",
    "CODE_PATH_NOT_ELIGIBLE",
    "InferencePathConfig",
    "PathDecision",
    "ExecutionIdentity",
    "Session11Adapter",
    "IdentityBoundFabric",
    "decide_path",
    "provenance_from_response",
]

#: The two paths a request can actually take.
PATH_LEGACY = "legacy"
PATH_SESSION11 = "session11"
#: The mode that mixes them by explicit configuration.
PATH_HYBRID = "hybrid"

VALID_MODES = (PATH_LEGACY, PATH_SESSION11, PATH_HYBRID)

#: Refusal codes. These are infrastructure verdicts, not model output.
CODE_PATH_UNAVAILABLE = "INFERENCE_PATH_UNAVAILABLE"
CODE_PATH_DISABLED = "INFERENCE_PATH_DISABLED"
CODE_PATH_NOT_ELIGIBLE = "INFERENCE_PATH_NOT_ELIGIBLE"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_tuple(name: str) -> Tuple[str, ...]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return ()
    parts = [item.strip() for item in raw.replace(";", ",").split(",")]
    return tuple(item for item in parts if item)


@dataclass
class InferencePathConfig:
    """Which inference path serves a request, and under what conditions.

    ``mode`` is the only switch that changes behaviour. Everything else is a
    bound that is passed down to the Session-11 fabric, which remains the single
    place that enforces verification, policy and resources.
    """

    mode: str = PATH_LEGACY
    #: Mirrors the server's ``require_verified``: a task that needs neural
    #: inference may not be served by a configured-but-unverified model.
    require_verified: bool = True
    #: May the deterministic (non-neural) rung answer at all?
    allow_deterministic: bool = True
    #: ``hybrid`` only: capabilities served by Session 11. Empty means every
    #: capability is eligible (the caller list below is then the only filter).
    hybrid_capabilities: Tuple[str, ...] = ()
    #: ``hybrid`` only: explicit callers that are *not* migrated yet, matched
    #: against ``ModelRequest.task``. Hybrid must be observable, so this list is
    #: the record of what still runs legacy and why.
    hybrid_legacy_callers: Tuple[str, ...] = ()
    #: Bounds forwarded to the Session-11 request when the caller set none.
    max_output_tokens: int = 0
    timeout_seconds: float = 0.0
    #: Classification forwarded when the caller did not declare one.
    classification: str = ""

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(
                "inference path mode must be one of %s, got %r"
                % (", ".join(VALID_MODES), self.mode))
        if self.max_output_tokens < 0:
            raise ValueError("max_output_tokens may not be negative")
        if self.timeout_seconds < 0:
            raise ValueError("timeout_seconds may not be negative")

    @classmethod
    def from_env(cls, defaults: Optional[Dict[str, Any]] = None
                 ) -> "InferencePathConfig":
        """Read the operator's choice. Unknown values fail loudly."""
        base: Dict[str, Any] = dict(defaults or {})
        mode = os.environ.get("FORGE_INFERENCE_PATH", "").strip().lower()
        if mode:
            base["mode"] = mode
        if os.environ.get("FORGE_INFERENCE_PATH_REQUIRE_VERIFIED", "").strip():
            base["require_verified"] = _env_flag(
                "FORGE_INFERENCE_PATH_REQUIRE_VERIFIED", True)
        if os.environ.get("FORGE_INFERENCE_PATH_ALLOW_DETERMINISTIC", "").strip():
            base["allow_deterministic"] = _env_flag(
                "FORGE_INFERENCE_PATH_ALLOW_DETERMINISTIC", True)
        capabilities = _env_tuple("FORGE_INFERENCE_PATH_HYBRID_CAPABILITIES")
        if capabilities:
            base["hybrid_capabilities"] = capabilities
        callers = _env_tuple("FORGE_INFERENCE_PATH_HYBRID_LEGACY_CALLERS")
        if callers:
            base["hybrid_legacy_callers"] = callers
        config = cls(**base)
        config.validate()
        return config

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "require_verified": bool(self.require_verified),
            "allow_deterministic": bool(self.allow_deterministic),
            "hybrid_capabilities": list(self.hybrid_capabilities),
            "hybrid_legacy_callers": list(self.hybrid_legacy_callers),
            "max_output_tokens": int(self.max_output_tokens),
            "timeout_seconds": float(self.timeout_seconds),
            "classification": self.classification,
        }


@dataclass
class PathDecision:
    """The recorded answer to "which path served this request, and why"."""

    #: The path that will be used: ``legacy`` or ``session11``.
    path: str = PATH_LEGACY
    #: The configured mode that produced it.
    mode: str = PATH_LEGACY
    #: False when the chosen path cannot serve the request (a refusal follows).
    eligible: bool = True
    reason: str = ""
    #: Refusal code when ``eligible`` is False.
    code: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "mode": self.mode,
                "eligible": bool(self.eligible), "reason": self.reason,
                "code": self.code}


def decide_path(config: Optional[InferencePathConfig], request: Any, *,
                available: bool = False,
                unavailable_reason: str = "") -> PathDecision:
    """Decide the path **before** any model work happens.

    Eligibility is a configuration question, never a reaction to failure: in
    ``hybrid`` mode a request is legacy because an operator said so, not because
    Session 11 misbehaved on a previous call.
    """
    cfg = config or InferencePathConfig()
    capability = str(getattr(request, "capability", "") or "")
    #: §22 — the stable component label wins; ``task`` is only a fallback for
    #: callers that predate the field (and is free text, so it is a poor key).
    caller = str(getattr(request, "caller", "") or "") or \
        str(getattr(request, "task", "") or "")
    metadata = getattr(request, "metadata", None) or {}
    forced = str(metadata.get("inference_path", "") or "").lower()

    if forced in (PATH_LEGACY, PATH_SESSION11):
        # An explicit per-request choice wins, and is recorded as such.
        if forced == PATH_SESSION11 and not available:
            return PathDecision(path=PATH_SESSION11, mode=cfg.mode,
                                eligible=False, code=CODE_PATH_UNAVAILABLE,
                                reason="session11 requested explicitly but the "
                                       "inference fabric is unavailable%s"
                                       % (": " + unavailable_reason
                                          if unavailable_reason else ""))
        return PathDecision(path=forced, mode=cfg.mode, eligible=True,
                            reason="explicit per-request inference_path=%s"
                                   % forced)

    if cfg.mode == PATH_LEGACY:
        return PathDecision(path=PATH_LEGACY, mode=cfg.mode, eligible=True,
                            reason="mode=legacy (Session-11 fabric not "
                                   "consulted)")

    if cfg.mode == PATH_SESSION11:
        if not available:
            return PathDecision(
                path=PATH_SESSION11, mode=cfg.mode, eligible=False,
                code=CODE_PATH_UNAVAILABLE,
                reason="mode=session11 but the inference fabric is unavailable"
                       "%s — refusing rather than silently routing legacy"
                       % (": " + unavailable_reason if unavailable_reason
                          else ""))
        return PathDecision(path=PATH_SESSION11, mode=cfg.mode, eligible=True,
                            reason="mode=session11 (canonical inference path)")

    # hybrid ---------------------------------------------------------------
    if caller and caller in cfg.hybrid_legacy_callers:
        return PathDecision(
            path=PATH_LEGACY, mode=cfg.mode, eligible=True,
            reason="mode=hybrid: caller %r is explicitly not migrated yet"
                   % caller)
    if cfg.hybrid_capabilities and capability not in cfg.hybrid_capabilities:
        return PathDecision(
            path=PATH_LEGACY, mode=cfg.mode, eligible=True,
            reason="mode=hybrid: capability %r is not in the Session-11 "
                   "allow-list %s" % (capability,
                                      list(cfg.hybrid_capabilities)))
    if not available:
        return PathDecision(
            path=PATH_LEGACY, mode=cfg.mode, eligible=True,
            reason="mode=hybrid: Session-11 fabric unavailable%s, so the "
                   "configured legacy path serves this request (recorded, not "
                   "silent)" % (": " + unavailable_reason
                                if unavailable_reason else ""))
    return PathDecision(path=PATH_SESSION11, mode=cfg.mode, eligible=True,
                        reason="mode=hybrid: capability %r is served by the "
                               "Session-11 fabric" % capability)


@dataclass
class ExecutionIdentity:
    """The identity a background generation carries across every boundary.

    Created where the truth is known — the worker that holds the lease — and
    stamped onto requests on the way down. Nothing here is reconstructed from a
    prompt, a timestamp or a previous attempt.
    """

    task_id: str = ""
    attempt_id: str = ""
    generation_id: str = ""
    request_id: str = ""
    trace_id: str = ""
    #: Filled in by the path that served the request (never by the caller).
    model_id: str = ""
    backend_id: str = ""
    provider_id: str = ""
    #: Lease/boot provenance: what makes a zombie worker detectable.
    lease_owner: str = ""
    boot_id: str = ""

    @classmethod
    def new(cls, task_id: str, *, attempt: Any = "", boot_id: str = "",
            lease_owner: str = "", trace_id: str = "") -> "ExecutionIdentity":
        """Mint an identity for one attempt of one task.

        ``attempt_id`` is unique per attempt (not the retry ordinal alone), so a
        retried task can never reuse a previous attempt's inference identity.
        """
        task = str(task_id or "")
        attempt_text = str(attempt or "")
        attempt_id = ("%s-%s" % (attempt_text, uuid4().hex[:8])
                      if attempt_text else uuid4().hex[:12])
        return cls(task_id=task, attempt_id=attempt_id,
                   trace_id=trace_id or uuid4().hex,
                   lease_owner=str(lease_owner or ""),
                   boot_id=str(boot_id or ""))

    def stamp(self, request: Any, *, bind_trace: bool = False) -> Any:
        """Fill identity fields the caller left empty.

        ``bind_trace`` is what an :class:`IdentityBoundFabric` passes: inside
        one execution attempt the attempt's trace is authoritative, so every
        request in that attempt shares it (§5). A ``ModelRequest`` mints a
        random ``trace_id`` of its own when nobody sets one, and letting each
        request keep a private trace would break correlation across the queue,
        the worker, the supervisor and the backend. Outside a bound view the
        caller's own trace id is left alone.
        """
        if request is None:
            return request
        for name, value in (("task_id", self.task_id),
                            ("attempt_id", self.attempt_id),
                            ("trace_id", self.trace_id),
                            ("generation_id", self.generation_id)):
            if not value:
                continue
            current = str(getattr(request, name, "") or "")
            if current and not (bind_trace and name == "trace_id"):
                continue
            try:
                setattr(request, name, value)
            except Exception:                         # noqa: BLE001 - frozen
                continue
        return request

    def with_generation(self, generation_id: str) -> "ExecutionIdentity":
        clone = ExecutionIdentity(**self.to_dict())
        clone.generation_id = str(generation_id or "")
        return clone

    def to_dict(self, *, include_lease: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "generation_id": self.generation_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "model_id": self.model_id,
            "backend_id": self.backend_id,
            "provider_id": self.provider_id,
        }
        if include_lease:
            payload["lease_owner"] = self.lease_owner
            payload["boot_id"] = self.boot_id
        return payload


class Session11Adapter:
    """The boundary between the legacy fabric vocabulary and Session 11.

    The inference fabric is obtained lazily through ``accessor`` so that
    constructing a fabric never touches a model directory, a socket or a
    provider. When the accessor cannot produce a fabric the adapter reports it
    as an unavailable path — it never fabricates a result and never hands the
    request to the legacy router behind the caller's back.
    """

    def __init__(self, accessor: Callable[[], Any], *,
                 config: Optional[InferencePathConfig] = None,
                 name: str = "session11",
                 fences: Any = None) -> None:
        if not callable(accessor):
            raise ValueError("a callable inference-fabric accessor is required")
        self._accessor = accessor
        self.config = config or InferencePathConfig()
        self.name = name or "session11"
        #: Optional attempt-fence authority, shared with the server so a
        #: generation and its task are fenced by the same registry.
        self.fences = fences
        self._counts: Dict[str, int] = {}
        self._history: List[Dict[str, Any]] = []
        self._history_limit = 50

    # -- availability ----------------------------------------------------

    def fabric(self) -> Any:
        """The live inference fabric (may raise; callers use :meth:`probe`)."""
        return self._accessor()

    def probe(self) -> Tuple[bool, str]:
        """``(available, redacted_reason)`` — never raises."""
        try:
            fabric = self._accessor()
        except Exception as exc:                      # noqa: BLE001
            return False, _redact(str(exc))[:300]
        if fabric is None:
            return False, "the inference fabric accessor returned nothing"
        return True, ""

    def decision(self, request: Any) -> PathDecision:
        available, reason = self.probe()
        return decide_path(self.config, request, available=available,
                           unavailable_reason=reason)

    # -- execution -------------------------------------------------------

    def generate(self, request: Any, *, decision: Optional[PathDecision] = None,
                 identity: Optional[ExecutionIdentity] = None,
                 fence: Any = None, fence_registry: Any = None,
                 commit_guard: Any = None) -> Any:
        """Run one generation through the Session-11 fabric.

        Returns a legacy ``ModelResponse`` so existing agents keep working; the
        Session-11 provenance rides in ``metadata`` (§21).
        """
        from forge.models.request import ModelResponse

        decision = decision or self.decision(request)
        identity = identity or ExecutionIdentity()
        request = identity.stamp(request)
        if not decision.eligible:
            self._count("refused")
            return self._refusal(request, decision, ModelResponse)
        try:
            fabric = self._accessor()
        except Exception as exc:                      # noqa: BLE001
            self._count("unavailable")
            return ModelResponse.failure(
                "the Session-11 inference fabric could not be obtained: %s"
                % _redact(str(exc))[:300],
                provider=self.name,
                request_id=str(getattr(request, "trace_id", "") or ""),
                metadata=self._metadata(request, decision, {},
                                        error_code=CODE_PATH_UNAVAILABLE))
        self._apply_bounds(request)
        kwargs: Dict[str, Any] = {
            "task_id": str(getattr(request, "task_id", "") or ""),
            "attempt_id": str(getattr(request, "attempt_id", "") or ""),
            "generation_id": str(getattr(request, "generation_id", "") or ""),
            "classification": str(getattr(request, "classification", "")
                                  or self.config.classification),
            "fence": fence,
            "fence_registry": fence_registry if fence_registry is not None
            else self.fences,
            "commit_guard": commit_guard,
        }
        started = _now_ms()
        try:
            result = fabric.generate(request, **kwargs)
        except Exception as exc:                      # noqa: BLE001
            #: A failure inside the canonical path stays a failure. Retrying it
            #: on the legacy router would be exactly the silent degradation this
            #: session exists to remove.
            self._count("error")
            return ModelResponse.failure(
                "Session-11 inference failed: %s" % _redact(str(exc))[:400],
                provider=self.name,
                request_id=str(getattr(request, "trace_id", "") or ""),
                latency_ms=_now_ms() - started,
                metadata=self._metadata(request, decision, {},
                                        error_code="INFERENCE_ERROR"))
        response = self.to_response(result, request=request,
                                    decision=decision)
        self._record(request, response, decision)
        return response

    def stream(self, request: Any, *,
               decision: Optional[PathDecision] = None,
               identity: Optional[ExecutionIdentity] = None,
               fence: Any = None, fence_registry: Any = None,
               commit_guard: Any = None) -> Iterator[str]:
        """Stream through the Session-11 fabric, yielding text deltas only.

        The terminal result is checked: a stream that ends in a failure or in a
        stale attempt raises instead of quietly looking like a success.
        """
        decision = decision or self.decision(request)
        identity = identity or ExecutionIdentity()
        request = identity.stamp(request)
        if not decision.eligible:
            self._count("refused")
            raise RuntimeError("%s: %s" % (decision.code or "INFERENCE_PATH",
                                           decision.reason))
        fabric = self._accessor()
        self._apply_bounds(request)
        handle = fabric.stream(
            request,
            task_id=str(getattr(request, "task_id", "") or ""),
            attempt_id=str(getattr(request, "attempt_id", "") or ""),
            generation_id=str(getattr(request, "generation_id", "") or ""),
            classification=str(getattr(request, "classification", "")
                               or self.config.classification),
            fence=fence,
            fence_registry=fence_registry if fence_registry is not None
            else self.fences,
            commit_guard=commit_guard)
        for event in handle.events():
            delta = getattr(event, "delta", "")
            if delta:
                yield delta
        final = handle.wait(1.0)
        if final is None:
            raise RuntimeError("Session-11 stream ended without a terminal "
                               "event (no result was published)")
        if not getattr(final, "success", False):
            state = str(getattr(final, "state", "failed"))
            code = str(getattr(final, "error_code", "") or "")
            raise RuntimeError("Session-11 stream finished %s%s: %s"
                               % (state, ("/" + code) if code else "",
                                  _redact(str(getattr(final, "error", "")
                                              or "no detail"))[:300]))

    def _apply_bounds(self, request: Any) -> None:
        """Apply the configured output/timeout bounds the caller did not set.

        Only fills gaps: an explicit bound from the caller always wins, and a
        bound of ``0`` in the config means "no opinion". The Session-11 fabric
        still clamps everything against the device profile, so this can widen
        nothing.
        """
        if request is None:
            return
        limit = int(self.config.max_output_tokens or 0)
        if limit > 0 and not getattr(request, "max_output_tokens", None):
            try:
                request.max_output_tokens = limit
            except Exception:                         # noqa: BLE001
                pass
        timeout = float(self.config.timeout_seconds or 0.0)
        if timeout > 0 and not getattr(request, "timeout", None):
            try:
                request.timeout = timeout
            except Exception:                         # noqa: BLE001
                pass

    # -- translation (§21) ------------------------------------------------

    def to_response(self, result: Any, *, request: Any = None,
                    decision: Optional[PathDecision] = None) -> Any:
        """Session-11 ``InferenceResult`` → legacy ``ModelResponse``.

        Nothing is dropped: model id, backend id, generation id, verification
        state, neural provenance and terminal state all survive in ``metadata``.
        """
        from forge.models.request import ModelResponse

        decision = decision or PathDecision(path=PATH_SESSION11,
                                            mode=self.config.mode,
                                            reason="session11")
        success = bool(getattr(result, "success", False))
        neural = bool(getattr(result, "neural", False))
        error = str(getattr(result, "error", "") or "")
        code = str(getattr(result, "error_code", "") or "")
        if not success and error:
            error = "%s%s" % (("[%s] " % code) if code else "", error)
        metadata = self._metadata(request, decision, result)
        return ModelResponse(
            text=str(getattr(result, "text", "") or ""),
            model=str(getattr(result, "model_id", "") or ""),
            provider=str(getattr(result, "provider", "")
                         or getattr(result, "backend_id", "") or self.name),
            success=success,
            error=_redact(error)[:500],
            request_id=str(getattr(result, "request_id", "")
                           or getattr(request, "trace_id", "") or ""),
            input_tokens=int(getattr(result, "input_tokens", 0) or 0),
            output_tokens=int(getattr(result, "output_tokens", 0) or 0),
            latency_ms=float(getattr(result, "latency_ms", 0.0) or 0.0),
            finish_reason=str(getattr(result, "finish_reason", "") or ""),
            raw=None,
            metadata=metadata)

    def _metadata(self, request: Any, decision: PathDecision, result: Any,
                  *, error_code: str = "") -> Dict[str, Any]:
        """Content-free provenance. No prompt, no context, no completion."""
        routing = getattr(result, "routing", None) or {}
        policy = getattr(result, "policy_result", None) or {}
        resource = getattr(result, "resource_result", None) or {}
        fallback = getattr(result, "fallback", None) or {}
        verification = getattr(result, "verification", None) or {}
        scan = getattr(result, "output_scan", None) or {}
        result_metadata = getattr(result, "metadata", None) or {}
        neural = bool(getattr(result, "neural", False)) if result else False
        code = error_code or str(getattr(result, "error_code", "") or "")
        residency = result_metadata.get("residency") or {}
        fingerprint = str(
            verification.get("fingerprint")
            or residency.get("fingerprint")
            or result_metadata.get("artifact_fingerprint") or "")
        metadata: Dict[str, Any] = {
            "inference_path": decision.path,
            "inference_mode": decision.mode,
            "inference_path_reason": decision.reason,
            "inference_path_eligible": bool(decision.eligible),
            "neural": neural,
            #: The distinction an agent must never lose.
            "deterministic": (not neural) if result is not None else False,
            "state": str(getattr(result, "state", "") or ""),
            "terminal_state": str(getattr(result, "terminal_state", "") or ""),
            "error_code": code,
            "model": str(getattr(result, "model_id", "") or ""),
            "backend_id": str(getattr(result, "backend_id", "") or ""),
            "provider_id": str(getattr(result, "provider", "") or ""),
            "generation_id": str(getattr(result, "generation_id", "") or ""),
            "request_id": str(getattr(result, "request_id", "") or ""),
            "task_id": str(getattr(result, "task_id", "")
                           or getattr(request, "task_id", "") or ""),
            "attempt_id": str(getattr(result, "attempt_id", "")
                              or getattr(request, "attempt_id", "") or ""),
            "trace_id": str(getattr(request, "trace_id", "") or ""),
            "verification_state": str(
                getattr(result, "verification_state", "") or ""),
            "availability_state": str(
                getattr(result, "availability_state", "") or ""),
            #: Provenance §20: which bytes actually answered, when known.
            "artifact_fingerprint": fingerprint[:128],
            "resident_bytes": residency.get("size_bytes"),
            "verified": bool(verification.get("verified"))
            if verification else False,
            "routing_reason": str(routing.get("reason", "") or "")[:400],
            "routing_score": routing.get("score"),
            "policy_allowed": policy.get("allowed"),
            "resource_allowed": resource.get("allowed"),
            "fallback_used": bool(fallback.get("used")),
            "fallback_rung": str(fallback.get("rung", "") or ""),
            "output_flags": list(scan.get("flags") or ()),
            "output_suspicious": bool(scan.get("suspicious")),
            "truncated": bool(getattr(result, "truncated", False)),
            "streamed": bool(getattr(result, "streamed", False)),
            "require_verified": bool(self.config.require_verified),
            #: §14 — the rest of the durable minimum: which fabric answered and
            #: how the generation ended, in bounded scalars. Timings and token
            #: counts are measurements, never content.
            "fabric_id": str(getattr(result, "fabric_id", "")
                             or result_metadata.get("fabric_id", "") or ""),
            "finish_reason": str(getattr(result, "finish_reason", "") or ""),
            "latency_ms": float(getattr(result, "latency_ms", 0.0) or 0.0),
            "input_tokens": int(getattr(result, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(result, "output_tokens", 0) or 0),
        }
        return metadata

    def _refusal(self, request: Any, decision: PathDecision,
                 response_cls: Any) -> Any:
        return response_cls.failure(
            decision.reason or "the Session-11 inference path refused this "
                               "request",
            provider=self.name,
            request_id=str(getattr(request, "trace_id", "") or ""),
            metadata=self._metadata(request, decision, {},
                                    error_code=decision.code
                                    or CODE_PATH_NOT_ELIGIBLE))

    # -- bounded observability (§33) --------------------------------------

    def _record(self, request: Any, response: Any,
                decision: PathDecision) -> None:
        metadata = getattr(response, "metadata", None) or {}
        self._count("served" if getattr(response, "success", False)
                    else "failed")
        entry = {
            "path": decision.path,
            "mode": decision.mode,
            "capability": str(getattr(request, "capability", "") or ""),
            "model": str(metadata.get("model", "") or ""),
            "backend_id": str(metadata.get("backend_id", "") or ""),
            "neural": bool(metadata.get("neural")),
            "success": bool(getattr(response, "success", False)),
            "state": str(metadata.get("state", "") or ""),
            "error_code": str(metadata.get("error_code", "") or ""),
            "verification_state": str(metadata.get("verification_state", "")
                                      or ""),
            "latency_ms": float(getattr(response, "latency_ms", 0.0) or 0.0),
            "task_id": str(metadata.get("task_id", "") or ""),
            "attempt_id": str(metadata.get("attempt_id", "") or ""),
            "generation_id": str(metadata.get("generation_id", "") or ""),
        }
        self._history.append(entry)
        if len(self._history) > self._history_limit:
            del self._history[:len(self._history) - self._history_limit]

    def note_legacy(self, request: Any, decision: PathDecision,
                    response: Any = None) -> None:
        """Record that the *legacy* path served a request.

        Hybrid mode is only trustworthy if the legacy calls are as visible as
        the Session-11 ones. Bounded and content-free like every other entry:
        capability, ids, outcome — never a prompt or a completion.
        """
        metadata = getattr(response, "metadata", None) or {}
        self._count("legacy_served")
        entry = {
            "path": PATH_LEGACY,
            "mode": decision.mode,
            "capability": str(getattr(request, "capability", "") or ""),
            "model": str(getattr(response, "model", "") or ""),
            "backend_id": "",
            "neural": bool(metadata.get("neural", True)),
            "success": bool(getattr(response, "success", False)),
            "state": "",
            #: The legacy vocabulary has no error code; an empty string is
            #: the honest value, never an invented one.
            "error_code": "",
            "verification_state": "",
            "latency_ms": float(getattr(response, "latency_ms", 0.0) or 0.0),
            "task_id": str(getattr(request, "task_id", "") or ""),
            "attempt_id": str(getattr(request, "attempt_id", "") or ""),
            "generation_id": "",
            "reason": decision.reason,
        }
        self._history.append(entry)
        if len(self._history) > self._history_limit:
            del self._history[:len(self._history) - self._history_limit]

    def _count(self, name: str) -> None:
        self._counts[name] = int(self._counts.get(name, 0)) + 1

    def history(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 20), self._history_limit))
        return list(self._history[-limit:])

    def snapshot(self) -> Dict[str, Any]:
        return {"name": self.name, "config": self.config.to_dict(),
                "counts": dict(self._counts),
                "history": self.history(limit=self._history_limit)}


class IdentityBoundFabric:
    """A fabric view that stamps one execution identity onto every request.

    Agents build their own :class:`ModelRequest` objects and know nothing about
    leases or attempts. Instead of asking them to, the worker's identity is
    bound once, here, at the boundary where it is known — and every request that
    passes through this view carries it. Nothing is inferred from the prompt.

    Attribute access falls through to the wrapped fabric, so callers that read
    ``fabric.router``/``fabric.registry``/``fabric.policy`` keep working.
    """

    def __init__(self, fabric: Any, identity: ExecutionIdentity, *,
                 fence: Any = None, fence_registry: Any = None,
                 commit_guard: Any = None) -> None:
        if fabric is None:
            raise ValueError("a fabric is required")
        self._fabric = fabric
        self.identity = identity or ExecutionIdentity()
        #: The attempt fence this execution is bound to. It travels *with* the
        #: request: a generation started for attempt N is checked against
        #: attempt N's fence before compute, at chunk boundaries and before
        #: publication, so a superseded attempt cannot publish.
        self.fence = fence
        self.fence_registry = fence_registry
        self.commit_guard = commit_guard

    # -- the call surface agents use --------------------------------------

    def _fence_kwargs(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Add this attempt's fence unless the caller supplied its own."""
        if self.fence is not None:
            kwargs.setdefault("fence", self.fence)
        if self.fence_registry is not None:
            kwargs.setdefault("fence_registry", self.fence_registry)
        if self.commit_guard is not None:
            kwargs.setdefault("commit_guard", self.commit_guard)
        return kwargs

    def generate(self, request: Any, **kwargs: Any) -> Any:
        return self._fabric.generate(self._stamp(request),
                                     **self._fence_kwargs(kwargs))

    def request(self, request: Any, **kwargs: Any) -> Any:
        return self._fabric.request(self._stamp(request),
                                    **self._fence_kwargs(kwargs))

    def stream(self, request: Any, **kwargs: Any) -> Any:
        return self._fabric.stream(self._stamp(request),
                                   **self._fence_kwargs(kwargs))

    def route(self, request: Any, **kwargs: Any) -> Any:
        return self._fabric.route(self._stamp(request), **kwargs)

    def select(self, request: Any = None, **kwargs: Any) -> Any:
        if request is not None:
            request = self._stamp(request)
        return self._fabric.select(request, **kwargs)

    def bind_identity(self, identity: ExecutionIdentity, **kwargs: Any
                      ) -> "IdentityBoundFabric":
        """Re-bind (a nested supervisor run, a sub-task) without losing scope."""
        kwargs.setdefault("fence", self.fence)
        kwargs.setdefault("fence_registry", self.fence_registry)
        kwargs.setdefault("commit_guard", self.commit_guard)
        return IdentityBoundFabric(self._fabric, identity, **kwargs)

    def _stamp(self, request: Any) -> Any:
        if isinstance(request, str):
            # Let the wrapped fabric build the request, then stamp it: the
            # capability default stays the fabric's decision, not ours.
            from forge.models.request import ModelRequest
            request = ModelRequest(prompt=request)
        return self.identity.stamp(request, bind_trace=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fabric, name)


#: The provenance keys that survive into a durable task result (§24). All are
#: bounded identifiers and verdicts — never a prompt, a context or a completion.
PROVENANCE_KEYS = (
    "inference_mode", "inference_path_reason", "neural", "deterministic",
    "state", "error_code", "model", "backend_id", "provider_id",
    "generation_id", "request_id", "task_id", "attempt_id", "trace_id",
    "verification_state", "availability_state", "artifact_fingerprint",
    "routing_reason", "routing_score", "policy_allowed", "resource_allowed",
    "fallback_used", "fallback_rung", "truncated", "streamed",
    #: §14 durable minimum: which fabric answered, how it ended, and what it
    #: cost in time and tokens. Bounded scalars — never prompt or completion.
    "fabric_id", "finish_reason", "latency_ms", "input_tokens",
    "output_tokens",
)


def provenance_from_response(response: Any) -> Dict[str, Any]:
    """A bounded, content-free provenance block for one fabric response.

    This is what a durable task result records about its inference source, so
    "which model answered, was it verified, was it neural, which path served
    it" stays answerable after the process is gone. A response that carries no
    path metadata is reported as legacy rather than dressed up.
    """
    metadata = getattr(response, "metadata", None) or {}
    provenance: Dict[str, Any] = {}
    for key in PROVENANCE_KEYS:
        if key in metadata:
            provenance[key] = metadata[key]
    path = str(metadata.get("inference_path", "") or "")
    provenance["path"] = path or PATH_LEGACY
    provenance["success"] = bool(getattr(response, "success", False))
    if not path:
        #: Nothing was stamped: report the legacy vocabulary honestly.
        provenance["model"] = str(getattr(response, "model", "") or "")
        provenance["provider"] = str(getattr(response, "provider", "") or "")
        provenance["neural"] = bool(metadata.get("neural", True))
        provenance["deterministic"] = not bool(metadata.get("neural", True))
    return provenance


def _now_ms() -> float:
    import time
    return time.perf_counter() * 1000.0


#: Patterns that must never reach a log, an event, an audit record or a report.
_REDACTION_MARKERS = (
    "api_key", "apikey", "api-key", "authorization", "bearer", "token",
    "password", "passwd", "secret", "credential", "access_key", "private_key",
)


def _redact(text: str) -> str:
    """Bounded, credential-safe text for error paths.

    Exceptions from a provider or a socket can contain a URL with embedded
    credentials or an authorization header. This drops everything after the
    first marker instead of trying to be clever about the shape of a secret.
    """
    if not text:
        return ""
    lowered = text.lower()
    cut = len(text)
    for marker in _REDACTION_MARKERS:
        index = lowered.find(marker)
        if index >= 0:
            cut = min(cut, index)
    if cut < len(text):
        text = text[:max(0, cut)].rstrip(" =:'\",;") + " [redacted]"
    return text
