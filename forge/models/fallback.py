"""The deterministic fallback ladder (Session 11).

Routing picks a model; the ladder decides what happens when that model cannot
answer. It is deterministic (same inputs → same order), it is honest (the
terminal state says what actually happened), and it never converts a failure
into fabricated output.

::

    preferred verified model
      -> secondary verified model
        -> local verified model
          -> deterministic non-neural strategy
            -> NEEDS_MODEL / RESOURCE_DENIED / POLICY_DENIED / FAILED

A provider failure, a network failure, a timeout, or an authentication failure
stays a failure: it is recorded against the step that produced it, and the
next step is tried only if the ladder allows it. Nothing in this module can
make an unavailable model look ready.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "FallbackLadder",
    "FallbackPlan",
    "FallbackStep",
    "FallbackTier",
    "TerminalState",
    "classify_error",
    "DETERMINISTIC_MODEL_ID",
]

#: The non-neural rung. It is the existing fabric fallback provider (a
#: deterministic no-op that refuses to synthesize), never a pretend model.
DETERMINISTIC_MODEL_ID = "deterministic:local-fallback"


class TerminalState(str, Enum):
    """The honest end state of an inference attempt."""

    SUCCEEDED = "succeeded"
    NEEDS_MODEL = "needs_model"
    RESOURCE_DENIED = "resource_denied"
    POLICY_DENIED = "policy_denied"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    STALE = "stale"
    UNVERIFIED = "unverified"


class FallbackTier(str, Enum):
    PREFERRED_VERIFIED = "preferred_verified"
    SECONDARY_VERIFIED = "secondary_verified"
    LOCAL_VERIFIED = "local_verified"
    DETERMINISTIC = "deterministic"
    TERMINAL = "terminal"


#: Error-code prefixes mapped onto terminal states. Everything else is FAILED.
_CODE_MAP: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("TIMEOUT", "DEADLINE", "WALL_TIME"), TerminalState.TIMEOUT.value),
    (("CANCEL", "ABANDONED", "SHUTDOWN"), TerminalState.CANCELLED.value),
    (("STALE", "FENCED", "SUPERSEDED"), TerminalState.STALE.value),
    (("RESOURCE", "RESIDENCY", "CAPACITY", "MEMORY", "OOM", "CONCURRENCY",
      "SCRATCH", "COST_BUDGET"), TerminalState.RESOURCE_DENIED.value),
    (("POLICY", "NETWORK_POLICY", "DATA_POLICY", "SSRF", "FORBIDDEN",
      "UNAUTHORIZED", "AUTH", "CREDENTIAL", "PERMISSION",
      "APPROVAL"), TerminalState.POLICY_DENIED.value),
    (("UNVERIFIED", "VERIFICATION", "FINGERPRINT", "SPOOF"),
     TerminalState.UNVERIFIED.value),
    (("NOT_FOUND", "NO_MODEL", "NEEDS_MODEL", "UNAVAILABLE"),
     TerminalState.NEEDS_MODEL.value),
)


def classify_error(error: str = "", *, code: str = "",
                   timed_out: bool = False, cancelled: bool = False,
                   fenced: bool = False) -> str:
    """Map a failure onto an honest terminal state."""
    if fenced:
        return TerminalState.STALE.value
    if cancelled:
        return TerminalState.CANCELLED.value
    if timed_out:
        return TerminalState.TIMEOUT.value
    haystack = ("%s %s" % (code or "", error or "")).upper()
    if not haystack.strip():
        return TerminalState.FAILED.value
    for codes, state in _CODE_MAP:
        for token in codes:
            if token in haystack:
                return state
    return TerminalState.FAILED.value


@dataclass
class FallbackStep:
    """One rung of the ladder, with what actually happened on it."""

    order: int
    tier: str
    model_id: str = ""
    backend_id: str = ""
    reason: str = ""
    attempted: bool = False
    #: ``""`` until attempted, then a :class:`TerminalState` value.
    outcome: str = ""
    error: str = ""
    error_code: str = ""
    latency_ms: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    #: True when the step is the deterministic non-neural strategy.
    neural: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": int(self.order),
            "tier": self.tier,
            "model_id": self.model_id,
            "backend_id": self.backend_id,
            "reason": self.reason[:300],
            "attempted": bool(self.attempted),
            "outcome": self.outcome,
            "error": self.error[:400],
            "error_code": self.error_code,
            "neural": bool(self.neural),
            "latency_ms": round(float(self.latency_ms or 0.0), 3),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": dict(self.metadata),
        }


@dataclass
class FallbackPlan:
    """The ladder plus its honest terminal state."""

    steps: List[FallbackStep] = field(default_factory=list)
    terminal_state: str = TerminalState.NEEDS_MODEL.value
    reason: str = ""
    #: Index of the step that produced the accepted result (-1 = none).
    accepted_step: int = -1
    fallback_used: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def succeeded(self) -> bool:
        return self.terminal_state == TerminalState.SUCCEEDED.value

    @property
    def attempted(self) -> List[FallbackStep]:
        return [step for step in self.steps if step.attempted]

    def record(self, order: int, *, outcome: str, error: str = "",
               error_code: str = "", latency_ms: float = 0.0,
               attempted: bool = True) -> None:
        for step in self.steps:
            if step.order != order:
                continue
            step.attempted = attempted
            step.outcome = outcome
            step.error = error[:400]
            step.error_code = error_code
            step.latency_ms = latency_ms
            step.finished_at = time.time()
            break

    def to_dict(self) -> Dict[str, Any]:
        return {
            "terminal_state": self.terminal_state,
            "reason": self.reason[:400],
            "succeeded": self.succeeded,
            "fallback_used": bool(self.fallback_used),
            "accepted_step": int(self.accepted_step),
            "steps": [step.to_dict() for step in self.steps],
            "attempted": len(self.attempted),
            "created_at": self.created_at,
        }


class FallbackLadder:
    """Builds the deterministic ladder from ranked candidates."""

    def __init__(self, *, allow_remote: bool = True, allow_paid: bool = True,
                 allow_deterministic: bool = True,
                 require_verified: bool = True,
                 max_steps: int = 6) -> None:
        self.allow_remote = bool(allow_remote)
        self.allow_paid = bool(allow_paid)
        self.allow_deterministic = bool(allow_deterministic)
        self.require_verified = bool(require_verified)
        self.max_steps = max(2, int(max_steps or 2))

    # -- construction ----------------------------------------------------

    def build(self, candidates: Sequence[Any], *,
              preferred_model_id: str = "",
              deterministic_model_id: str = DETERMINISTIC_MODEL_ID,
              note: str = "") -> FallbackPlan:
        """Order candidates into tiers.

        ``candidates`` are :class:`~forge.models.identity.ModelIdentity`
        objects (or anything exposing ``model_id``, ``backend_id``, ``local``,
        ``free``, ``verified``/``verification_state`` and ``quality``), already
        filtered by capability and policy.
        """
        preferred: List[FallbackStep] = []
        secondary: List[FallbackStep] = []
        local: List[FallbackStep] = []
        seen: Dict[str, bool] = {}

        for entry in candidates:
            model_id = str(getattr(entry, "model_id", "") or "")
            if not model_id or model_id in seen:
                continue
            seen[model_id] = True
            backend_id = str(getattr(entry, "backend_id", "") or "")
            is_local = bool(getattr(entry, "local", True))
            verified = _is_verified(entry)
            if self.require_verified and not verified:
                continue
            if not is_local and not self.allow_remote:
                continue
            if not bool(getattr(entry, "free", True)) and not self.allow_paid:
                continue
            step = FallbackStep(order=0, tier="", model_id=model_id,
                                backend_id=backend_id,
                                reason=_candidate_reason(entry, verified))
            if model_id == preferred_model_id and not preferred:
                step.tier = FallbackTier.PREFERRED_VERIFIED.value
                step.reason = "preferred selection (%s)" % step.reason
                preferred.append(step)
            elif is_local:
                step.tier = FallbackTier.LOCAL_VERIFIED.value
                local.append(step)
            else:
                step.tier = FallbackTier.SECONDARY_VERIFIED.value
                secondary.append(step)

        steps: List[FallbackStep] = []
        for group in (preferred, secondary, local):
            steps.extend(group)
        if self.allow_deterministic:
            steps.append(FallbackStep(
                order=0, tier=FallbackTier.DETERMINISTIC.value,
                model_id=deterministic_model_id, backend_id="deterministic",
                neural=False,
                reason=("deterministic non-neural strategy; refuses to "
                        "synthesize model output")))
        for order, step in enumerate(steps[:self.max_steps], start=1):
            step.order = order
        plan = FallbackPlan(steps=steps[:self.max_steps])
        if not plan.steps:
            plan.terminal_state = TerminalState.NEEDS_MODEL.value
            plan.reason = note or ("no verified model satisfies the request; "
                                   "nothing will be fabricated")
        elif note:
            plan.reason = note
        return plan

    # -- terminal state --------------------------------------------------

    def terminal(self, plan: FallbackPlan, *, error: str = "",
                 code: str = "", timed_out: bool = False,
                 cancelled: bool = False, fenced: bool = False) -> str:
        """Decide (and record) the honest terminal state for a finished run."""
        state = classify_error(error, code=code, timed_out=timed_out,
                               cancelled=cancelled, fenced=fenced)
        plan.terminal_state = state
        if not plan.reason:
            plan.reason = error[:400] or state
        return state


def _is_verified(entry: Any) -> bool:
    value = getattr(entry, "verified", None)
    if isinstance(value, bool):
        return value
    state = str(getattr(entry, "verification_state", "") or "")
    return state == "verified"


def _candidate_reason(entry: Any, verified: bool) -> str:
    parts = ["local" if getattr(entry, "local", True) else "remote"]
    parts.append("free" if getattr(entry, "free", True) else "paid")
    parts.append("verified" if verified else "unverified")
    quality = getattr(entry, "quality", None)
    if isinstance(quality, (int, float)) and quality:
        parts.append("quality=%.2f" % float(quality))
    reliability = getattr(entry, "reliability", None)
    if isinstance(reliability, (int, float)):
        parts.append("reliability=%.2f" % float(reliability))
    return ", ".join(parts)
