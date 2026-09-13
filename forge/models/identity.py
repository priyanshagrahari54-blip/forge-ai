"""Model identity contract (Session 11).

A *model identity* is the structured, verifiable description of one usable
model. Everything above the backend layer — the catalog, the routing engine,
the residency cache, the server API, the CLI — speaks in identities rather
than in provider-specific dictionaries, so a model can never be substituted
silently and a state can never be claimed without evidence.

Two orthogonal axes are tracked, because conflating them is how systems end
up reporting a model as ready when nothing was ever verified:

``availability_state``
    Where the model is in its lifecycle: DISCOVERED, CONFIGURED, LOADED,
    READY, DEGRADED, UNAVAILABLE, FAILED, POLICY_DENIED, RESOURCE_DENIED,
    UNVERIFIED.

``verification_state``
    Whether an *actual* check proved this model is the model it claims to be
    and can produce output: UNVERIFIED, VERIFIED, FAILED, EXPIRED.

Hard rules (enforced in code, not documentation):

* ``CONFIGURED -> READY`` is refused unless the identity is VERIFIED. A
  configuration entry, an API key, or a file on disk is *not* availability.
* ``READY`` always implies VERIFIED; verification expiry demotes to
  ``UNVERIFIED`` rather than leaving a stale READY in place.
* :meth:`ModelIdentity.assert_same_model` refuses a response whose identity
  does not match the request (model spoofing / silent substitution).
* Unknown values stay empty or ``None``. Nothing here invents a context
  limit, a parameter count, or a fingerprint.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "AvailabilityState",
    "IdentityError",
    "MemoryRequirements",
    "ModelIdentity",
    "ModelSpoofingError",
    "VerificationState",
    "availability_from_runtime_state",
]


class AvailabilityState(str, Enum):
    """Lifecycle state of one model identity."""

    DISCOVERED = "discovered"
    CONFIGURED = "configured"
    LOADED = "loaded"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    POLICY_DENIED = "policy_denied"
    RESOURCE_DENIED = "resource_denied"
    UNVERIFIED = "unverified"


class VerificationState(str, Enum):
    """Whether an actual check proved this identity."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"
    EXPIRED = "expired"


#: States that mean "this model may be selected and invoked".
USABLE_STATES: Tuple[str, ...] = (
    AvailabilityState.READY.value,
    AvailabilityState.LOADED.value,
    AvailabilityState.DEGRADED.value,
)

#: States that are a *refusal*, not a failure of the model itself.
DENIAL_STATES: Tuple[str, ...] = (
    AvailabilityState.POLICY_DENIED.value,
    AvailabilityState.RESOURCE_DENIED.value,
)

#: Terminal-ish negative states: routing must not select these.
NEGATIVE_STATES: Tuple[str, ...] = (
    AvailabilityState.UNAVAILABLE.value,
    AvailabilityState.FAILED.value,
    AvailabilityState.UNVERIFIED.value,
) + DENIAL_STATES

#: Transitions that require an already-VERIFIED identity.
_VERIFICATION_GATED: Tuple[str, ...] = (
    AvailabilityState.READY.value,
)

#: Transitions allowed from any state (refusals and failures are always
#: reachable: the system must be able to record a denial at any time).
_ALWAYS_ALLOWED: Tuple[str, ...] = (
    AvailabilityState.FAILED.value,
    AvailabilityState.POLICY_DENIED.value,
    AvailabilityState.RESOURCE_DENIED.value,
    AvailabilityState.UNAVAILABLE.value,
    AvailabilityState.UNVERIFIED.value,
    AvailabilityState.DEGRADED.value,
)

#: Forward lifecycle edges. Anything not listed here and not always-allowed
#: is refused, so a state can never be *promoted* by accident.
_ALLOWED_EDGES: Dict[str, Tuple[str, ...]] = {
    AvailabilityState.DISCOVERED.value: (
        AvailabilityState.CONFIGURED.value,
        AvailabilityState.LOADED.value,
    ),
    AvailabilityState.CONFIGURED.value: (
        AvailabilityState.LOADED.value,
        AvailabilityState.READY.value,       # verification-gated
    ),
    AvailabilityState.LOADED.value: (
        AvailabilityState.READY.value,       # verification-gated
    ),
    AvailabilityState.READY.value: (
        AvailabilityState.LOADED.value,
        AvailabilityState.CONFIGURED.value,
        AvailabilityState.DISCOVERED.value,
    ),
    AvailabilityState.DEGRADED.value: (
        AvailabilityState.READY.value,       # verification-gated
        AvailabilityState.LOADED.value,
        AvailabilityState.CONFIGURED.value,
    ),
    AvailabilityState.UNVERIFIED.value: (
        AvailabilityState.CONFIGURED.value,
        AvailabilityState.LOADED.value,
        AvailabilityState.READY.value,       # verification-gated
    ),
    AvailabilityState.UNAVAILABLE.value: (
        AvailabilityState.DISCOVERED.value,
        AvailabilityState.CONFIGURED.value,
    ),
    AvailabilityState.FAILED.value: (
        AvailabilityState.DISCOVERED.value,
        AvailabilityState.CONFIGURED.value,
    ),
    AvailabilityState.POLICY_DENIED.value: (
        AvailabilityState.CONFIGURED.value,
        AvailabilityState.DISCOVERED.value,
    ),
    AvailabilityState.RESOURCE_DENIED.value: (
        AvailabilityState.CONFIGURED.value,
        AvailabilityState.DISCOVERED.value,
    ),
}


class IdentityError(RuntimeError):
    """An illegal identity transition or an invalid identity."""

    code = "IDENTITY"

    def __init__(self, message: str, *, code: str = "IDENTITY") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ModelSpoofingError(IdentityError):
    """A backend answered with a different model than the one requested."""

    code = "MODEL_SPOOFED"

    def __init__(self, message: str) -> None:
        super().__init__(message, code="MODEL_SPOOFED")


@dataclass(frozen=True)
class MemoryRequirements:
    """What a model needs to be resident. Zero/None means *not reported*."""

    weights_bytes: int = 0
    resident_bytes: int = 0
    min_available_mb: int = 0
    #: ``True`` only when a real measurement (artifact size, backend report)
    #: produced these numbers. Never ``True`` for a guessed value.
    measured: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "weights_bytes": int(self.weights_bytes or 0),
            "resident_bytes": int(self.resident_bytes or 0),
            "min_available_mb": int(self.min_available_mb or 0),
            "measured": bool(self.measured),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MemoryRequirements":
        data = data or {}
        return cls(
            weights_bytes=int(data.get("weights_bytes") or 0),
            resident_bytes=int(data.get("resident_bytes") or 0),
            min_available_mb=int(data.get("min_available_mb") or 0),
            measured=bool(data.get("measured", False)),
        )


@dataclass
class ModelIdentity:
    """The structured identity contract for one usable model.

    ``model_id`` is the canonical key (``"<backend>:<name>"`` for runtime
    models). Every field is either read from an artifact, reported by a
    backend, or left empty — this class never fills in a plausible default.
    """

    model_id: str
    provider_id: str = ""
    backend_id: str = ""
    model_family: str = ""
    model_version: str = ""
    #: 0 means "not reported" (never a fabricated context window).
    context_limit: int = 0
    max_output_limit: int = 0
    capabilities: Tuple[str, ...] = ()
    quantization: str = ""
    #: ``None`` when unknown; a string such as ``"7B"`` may live in metadata.
    parameter_count: Optional[int] = None
    parameter_label: str = ""
    #: ``True`` for a local artifact/endpoint, ``False`` for a remote provider.
    local: bool = True
    memory_requirements: MemoryRequirements = field(
        default_factory=MemoryRequirements)
    supported_platforms: Tuple[str, ...] = ()
    availability_state: str = AvailabilityState.DISCOVERED.value
    verification_state: str = VerificationState.UNVERIFIED.value
    #: Content fingerprint of the local artifact ("" when remote/unknown).
    artifact_fingerprint: str = ""
    artifact_path: str = ""
    artifact_format: str = ""
    size_bytes: int = 0
    free: bool = True
    cost_per_token: float = 0.0
    #: Routing-quality signal in [0, 1]; 0.0 means "no evidence".
    quality: float = 0.0
    #: Latency evidence in ms; 0.0 means "no evidence".
    latency_ms: float = 0.0
    #: Reliability evidence in [0, 1]; 1.0 is the neutral starting point.
    reliability: float = 1.0
    verified_at: float = 0.0
    verification_method: str = ""
    verification_detail: str = ""
    #: Seconds after which a verification is considered stale (0 = never).
    verification_ttl_seconds: float = 0.0
    updated_at: float = field(default_factory=time.time)
    last_health: Dict[str, Any] = field(default_factory=dict)
    #: Bounded, human-readable trail of state transitions (never content).
    history: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model_id or not str(self.model_id).strip():
            raise IdentityError("model_id is required")
        self.model_id = str(self.model_id).strip()
        self.capabilities = tuple(self.capabilities or ())
        self.supported_platforms = tuple(self.supported_platforms or ())
        if not self.backend_id and ":" in self.model_id:
            self.backend_id = self.model_id.split(":", 1)[0]
        if self.availability_state not in {s.value for s in AvailabilityState}:
            raise IdentityError(
                "unknown availability_state %r" % (self.availability_state,))
        if self.verification_state not in {s.value for s in VerificationState}:
            raise IdentityError(
                "unknown verification_state %r" % (self.verification_state,))
        if self.availability_state == AvailabilityState.READY.value \
                and self.verification_state != VerificationState.VERIFIED.value:
            # Never construct a READY identity that was not verified.
            self.availability_state = AvailabilityState.UNVERIFIED.value

    # -- derived views ---------------------------------------------------

    @property
    def usable(self) -> bool:
        """Is the *availability* state selectable right now?

        This is one of two gates, and it deliberately does not fold the other
        one in: the router also refuses an identity whose
        :attr:`verification_state` is not ``verified`` when the request
        requires verification (the default). Keeping them separate is what
        makes a refusal explainable — "not available" and "not verified" are
        different facts with different remedies.
        """
        self.expire_verification()
        return self.availability_state in USABLE_STATES

    @property
    def denied(self) -> bool:
        return self.availability_state in DENIAL_STATES

    @property
    def verified(self) -> bool:
        self.expire_verification()
        return self.verification_state == VerificationState.VERIFIED.value

    @property
    def name(self) -> str:
        """Bare model name (``model_id`` without the backend prefix)."""
        return self.model_id.split(":", 1)[-1]

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def supports_all(self, capabilities: Any) -> bool:
        required = tuple(capabilities or ())
        return all(item in self.capabilities for item in required)

    # -- state machine ---------------------------------------------------

    def set_availability(self, state: str, *, reason: str = "",
                         strict: bool = True) -> "ModelIdentity":
        """Move to ``state``, enforcing the transition rules.

        ``strict=True`` (the default) raises :class:`IdentityError` for an
        illegal promotion, so a caller cannot quietly mark a configured model
        READY. ``strict=False`` records the refusal in ``history`` and demotes
        to UNVERIFIED instead of raising — used by discovery paths that scan
        many identities and must not abort on one bad entry.
        """
        if state not in {item.value for item in AvailabilityState}:
            raise IdentityError("unknown availability_state %r" % (state,))
        current = self.availability_state
        if state == current:
            if reason:
                self._record(current, reason)
            return self

        gated = state in _VERIFICATION_GATED
        if gated and self.verification_state != VerificationState.VERIFIED.value:
            message = ("cannot promote %s to %s: verification_state is %r "
                       "(CONFIGURED is never READY without real verification)"
                       % (current, state, self.verification_state))
            if strict:
                raise IdentityError(message, code="UNVERIFIED_PROMOTION")
            self._record(state, "REFUSED: " + message)
            self.availability_state = AvailabilityState.UNVERIFIED.value
            self.updated_at = time.time()
            return self

        allowed = _ALWAYS_ALLOWED + _ALLOWED_EDGES.get(current, ())
        if state not in allowed:
            message = ("illegal availability transition %s -> %s"
                       % (current, state))
            if strict:
                raise IdentityError(message, code="ILLEGAL_TRANSITION")
            self._record(state, "REFUSED: " + message)
            return self

        self.availability_state = state
        self.updated_at = time.time()
        self._record(state, reason)
        return self

    def set_verification(self, state: str, *, method: str = "",
                         detail: str = "", fingerprint: str = "",
                         ttl_seconds: Optional[float] = None) -> "ModelIdentity":
        """Record a verification outcome and keep availability coherent.

        A verification failure always demotes: a READY model that fails a
        probe becomes FAILED, never stays READY.
        """
        if state not in {item.value for item in VerificationState}:
            raise IdentityError("unknown verification_state %r" % (state,))
        self.verification_state = state
        if method:
            self.verification_method = method
        if detail:
            self.verification_detail = detail[:500]
        if fingerprint:
            self.artifact_fingerprint = fingerprint
        if ttl_seconds is not None:
            self.verification_ttl_seconds = max(0.0, float(ttl_seconds))
        if state == VerificationState.VERIFIED.value:
            self.verified_at = time.time()
            if self.availability_state in (
                    AvailabilityState.UNVERIFIED.value,
                    AvailabilityState.DISCOVERED.value):
                self.availability_state = AvailabilityState.CONFIGURED.value
                self._record(self.availability_state, "verified")
        elif state in (VerificationState.FAILED.value,
                       VerificationState.EXPIRED.value):
            self.verified_at = 0.0
            if self.availability_state in (AvailabilityState.READY.value,
                                           AvailabilityState.LOADED.value):
                previous = self.availability_state
                self.availability_state = (
                    AvailabilityState.FAILED.value
                    if state == VerificationState.FAILED.value
                    else AvailabilityState.UNVERIFIED.value)
                self._record(self.availability_state,
                             "demoted from %s: verification %s"
                             % (previous, state))
        self.updated_at = time.time()
        return self

    def expire_verification(self) -> bool:
        """Demote a verification that is older than its TTL. Returns changed."""
        ttl = float(self.verification_ttl_seconds or 0.0)
        if ttl <= 0.0:
            return False
        if self.verification_state != VerificationState.VERIFIED.value:
            return False
        if (time.time() - float(self.verified_at or 0.0)) <= ttl:
            return False
        self.set_verification(VerificationState.EXPIRED.value,
                              detail="verification older than %.0fs" % ttl)
        return True

    def record_health(self, health: Dict[str, Any]) -> None:
        """Store the last health result (bounded, metadata only)."""
        bounded = {str(key): health[key] for key in list(health)[:24]}
        self.last_health = bounded
        status = str(health.get("status") or "")
        if status == "ready" and self.verified \
                and self.availability_state in (
                    AvailabilityState.LOADED.value,
                    AvailabilityState.CONFIGURED.value,
                    AvailabilityState.DEGRADED.value):
            self.availability_state = AvailabilityState.READY.value
            self._record(self.availability_state, "health=ready")
        elif status in ("degraded",) and self.availability_state in USABLE_STATES:
            self.availability_state = AvailabilityState.DEGRADED.value
            self._record(self.availability_state, "health=degraded")
        elif status in ("unavailable", "failed") \
                and self.availability_state not in DENIAL_STATES:
            self.availability_state = AvailabilityState.UNAVAILABLE.value
            self._record(self.availability_state, "health=%s" % status)
        self.updated_at = time.time()

    def _record(self, state: str, reason: str) -> None:
        self.history.append({"state": state, "reason": (reason or "")[:300],
                             "at": time.time()})
        if len(self.history) > 32:
            self.history = self.history[-32:]

    # -- anti-substitution ------------------------------------------------

    def assert_same_model(self, *, reported_model: str = "",
                          reported_fingerprint: str = "",
                          reported_backend: str = "") -> None:
        """Refuse a response that did not come from *this* model.

        Empty reported values are treated as "the backend did not say", which
        is not a mismatch. A *different* non-empty value is.
        """
        reported = (reported_model or "").strip()
        if reported:
            candidates = {self.model_id, self.name}
            if self.metadata.get("alias"):
                candidates.add(str(self.metadata["alias"]))
            if reported not in candidates:
                raise ModelSpoofingError(
                    "backend answered with model %r but %r was requested"
                    % (reported[:200], self.model_id))
        if reported_backend and self.backend_id \
                and reported_backend != self.backend_id:
            raise ModelSpoofingError(
                "backend %r answered for a model registered on backend %r"
                % (reported_backend[:100], self.backend_id))
        if reported_fingerprint and self.artifact_fingerprint \
                and reported_fingerprint != self.artifact_fingerprint:
            raise ModelSpoofingError(
                "artifact fingerprint %s does not match the registered %s"
                % (reported_fingerprint[:64], self.artifact_fingerprint[:64]))

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """A loggable view: metadata only, never prompt/response content."""
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "backend_id": self.backend_id,
            "model_family": self.model_family,
            "model_version": self.model_version,
            "context_limit": int(self.context_limit or 0),
            "max_output_limit": int(self.max_output_limit or 0),
            "capabilities": list(self.capabilities),
            "quantization": self.quantization,
            "parameter_count": self.parameter_count,
            "parameter_label": self.parameter_label,
            "local_or_remote": "local" if self.local else "remote",
            "local": bool(self.local),
            "memory_requirements": self.memory_requirements.to_dict(),
            "supported_platforms": list(self.supported_platforms),
            "availability_state": self.availability_state,
            "verification_state": self.verification_state,
            "artifact_fingerprint": self.artifact_fingerprint,
            "artifact_format": self.artifact_format,
            "size_bytes": int(self.size_bytes or 0),
            "free": bool(self.free),
            "cost_per_token": float(self.cost_per_token or 0.0),
            "quality": round(float(self.quality or 0.0), 4),
            "latency_ms": round(float(self.latency_ms or 0.0), 3),
            "reliability": round(float(self.reliability or 0.0), 4),
            "verified_at": self.verified_at,
            "verification_method": self.verification_method,
            "updated_at": self.updated_at,
            "last_health": dict(self.last_health),
            "history": [dict(item) for item in self.history[-8:]],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelIdentity":
        data = data or {}
        return cls(
            model_id=str(data.get("model_id") or ""),
            provider_id=str(data.get("provider_id") or ""),
            backend_id=str(data.get("backend_id") or ""),
            model_family=str(data.get("model_family") or ""),
            model_version=str(data.get("model_version") or ""),
            context_limit=int(data.get("context_limit") or 0),
            max_output_limit=int(data.get("max_output_limit") or 0),
            capabilities=tuple(data.get("capabilities") or ()),
            quantization=str(data.get("quantization") or ""),
            parameter_count=data.get("parameter_count"),
            parameter_label=str(data.get("parameter_label") or ""),
            local=bool(data.get("local", data.get("local_or_remote", "local")
                       == "local")),
            memory_requirements=MemoryRequirements.from_dict(
                data.get("memory_requirements")),
            supported_platforms=tuple(data.get("supported_platforms") or ()),
            availability_state=str(
                data.get("availability_state")
                or AvailabilityState.DISCOVERED.value),
            verification_state=str(
                data.get("verification_state")
                or VerificationState.UNVERIFIED.value),
            artifact_fingerprint=str(data.get("artifact_fingerprint") or ""),
            artifact_format=str(data.get("artifact_format") or ""),
            size_bytes=int(data.get("size_bytes") or 0),
            free=bool(data.get("free", True)),
            cost_per_token=float(data.get("cost_per_token") or 0.0),
            quality=float(data.get("quality") or 0.0),
            latency_ms=float(data.get("latency_ms") or 0.0),
            reliability=float(data.get("reliability", 1.0)),
            verified_at=float(data.get("verified_at") or 0.0),
            verification_method=str(data.get("verification_method") or ""),
            metadata=dict(data.get("metadata") or {}),
        )

    # -- conversions ------------------------------------------------------

    @classmethod
    def from_runtime_model(cls, runtime_model: Any, *,
                           provider_id: str = "",
                           capabilities: Tuple[str, ...] = (),
                           free: bool = True,
                           cost_per_token: float = 0.0,
                           supported_platforms: Tuple[str, ...] = (),
                           metadata: Optional[Dict[str, Any]] = None,
                           ) -> "ModelIdentity":
        """Build an identity from a runtime ``RuntimeModel``.

        Only fields the runtime actually reported are carried over; a zero
        context window stays zero rather than becoming a guess.
        """
        size_bytes = int(getattr(runtime_model, "size_bytes", 0) or 0)
        rmeta = dict(getattr(runtime_model, "metadata", {}) or {})
        merged: Dict[str, Any] = dict(rmeta)
        merged.update(dict(metadata or {}))
        parameters = str(getattr(runtime_model, "parameters", "") or "")
        identity = cls(
            model_id=str(getattr(runtime_model, "model_id", "") or ""),
            provider_id=provider_id or str(
                getattr(runtime_model, "backend", "") or ""),
            backend_id=str(getattr(runtime_model, "backend", "") or ""),
            model_family=str(rmeta.get("family") or rmeta.get("architecture")
                             or ""),
            model_version=str(rmeta.get("version") or ""),
            context_limit=int(getattr(runtime_model, "context_window", 0) or 0),
            max_output_limit=int(
                getattr(runtime_model, "max_output_tokens", 0) or 0),
            capabilities=tuple(capabilities or getattr(
                runtime_model, "capabilities", ()) or ()),
            quantization=str(getattr(runtime_model, "quantization", "") or ""),
            parameter_count=_parameter_count_from(rmeta, parameters),
            parameter_label=parameters,
            local=bool(getattr(runtime_model, "local", True)),
            memory_requirements=MemoryRequirements(
                weights_bytes=size_bytes,
                resident_bytes=int(rmeta.get("resident_bytes") or size_bytes),
                min_available_mb=int((size_bytes // (1024 * 1024)) * 1.2)
                if size_bytes else 0,
                measured=bool(size_bytes)),
            supported_platforms=tuple(supported_platforms or ()),
            availability_state=(AvailabilityState.LOADED.value
                                if getattr(runtime_model, "loaded", False)
                                else AvailabilityState.DISCOVERED.value),
            verification_state=VerificationState.UNVERIFIED.value,
            artifact_fingerprint=str(rmeta.get("fingerprint") or ""),
            artifact_path=str(getattr(runtime_model, "path", "") or ""),
            artifact_format=str(getattr(runtime_model, "format", "") or ""),
            size_bytes=size_bytes,
            free=bool(free),
            cost_per_token=float(cost_per_token or 0.0),
            metadata=merged,
        )
        return identity


def _parameter_count_from(metadata: Dict[str, Any],
                          label: str) -> Optional[int]:
    """A real parameter count when one is reported; ``None`` otherwise."""
    value = metadata.get("parameter_count")
    if isinstance(value, int) and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return _parse_parameter_count(label)


def _parse_parameter_count(label: str) -> Optional[int]:
    """Parse ``"7B"``/``"1.5b"``/``"7000000000"`` into a count, else None."""
    text = (label or "").strip().lower()
    if not text:
        return None
    try:
        if text.endswith("b"):
            return int(float(text[:-1]) * 1_000_000_000)
        if text.endswith("m"):
            return int(float(text[:-1]) * 1_000_000)
        if text.endswith("k"):
            return int(float(text[:-1]) * 1_000)
        return int(float(text))
    except ValueError:
        return None


def availability_from_runtime_state(status: str, *,
                                    verified: bool = False) -> str:
    """Map a runtime/backend health status onto an availability state."""
    text = (status or "").strip().lower()
    if text == "ready":
        return (AvailabilityState.READY.value if verified
                else AvailabilityState.UNVERIFIED.value)
    if text == "degraded":
        return AvailabilityState.DEGRADED.value
    if text in ("unavailable", "unknown"):
        return AvailabilityState.UNAVAILABLE.value
    return AvailabilityState.FAILED.value
