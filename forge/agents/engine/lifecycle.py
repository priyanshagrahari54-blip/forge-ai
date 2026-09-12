"""Agent lifecycle (A81): explicit states that gate execution.

States and the only transitions allowed between them::

    created ──validate──▶ validated ──benchmark──▶ tested ──▶ enabled
                                                              │    ▲
                                                        pause │    │ resume
                                                              ▼    │
                                                            paused
                                                              │
    validated / tested / enabled / paused ──disable──▶ disabled
    disabled ──revalidate──▶ validated        disabled ──▶ retired (final)

Every transition is recorded with the actor and a reason, so a package
carries its own history. Two properties matter most:

* **Evidence gates promotion.** ``tested`` requires a recorded benchmark
  report that met the spec's verification requirements, and ``enabled``
  is only reachable from ``tested``. Nothing can be enabled on a claim.
* **Changing the spec invalidates the evidence.** Re-specifying an agent
  returns it to ``created``; the old validation and benchmark results no
  longer describe the code that will run.

``retired`` is terminal: a retired agent cannot be re-enabled, and its
package is kept for audit rather than reused.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from forge.agents.engine.errors import AgentLifecycleError


class AgentState:
    """Lifecycle states (plain strings so packages stay plain JSON)."""

    CREATED = "created"
    VALIDATED = "validated"
    TESTED = "tested"
    ENABLED = "enabled"
    PAUSED = "paused"
    DISABLED = "disabled"
    RETIRED = "retired"

    ALL: tuple = (CREATED, VALIDATED, TESTED, ENABLED, PAUSED, DISABLED,
                  RETIRED)

    #: States in which an agent may be run.
    RUNNABLE: tuple = (ENABLED,)


#: Legal transitions, ``from -> (to, ...)``.
TRANSITIONS: dict = {
    AgentState.CREATED: (AgentState.VALIDATED, AgentState.DISABLED),
    AgentState.VALIDATED: (AgentState.TESTED, AgentState.DISABLED),
    AgentState.TESTED: (AgentState.ENABLED, AgentState.DISABLED),
    AgentState.ENABLED: (AgentState.PAUSED, AgentState.DISABLED),
    AgentState.PAUSED: (AgentState.ENABLED, AgentState.DISABLED),
    AgentState.DISABLED: (AgentState.VALIDATED, AgentState.RETIRED),
    AgentState.RETIRED: (),
}

#: Transition that discards validation/benchmark evidence because the
#: specification changed. Reachable from every non-terminal state.
RESPEC_TARGET = AgentState.CREATED


@dataclass(frozen=True)
class Transition:
    """One recorded lifecycle change."""

    from_state: str
    to_state: str
    actor: str
    reason: str
    at: float

    def to_dict(self) -> dict:
        return {"from": self.from_state, "to": self.to_state,
                "actor": self.actor, "reason": self.reason, "at": self.at}

    @classmethod
    def from_dict(cls, payload: dict) -> "Transition":
        return cls(from_state=str(payload.get("from", "")),
                   to_state=str(payload.get("to", "")),
                   actor=str(payload.get("actor", "")),
                   reason=str(payload.get("reason", "")),
                   at=float(payload.get("at", 0.0)))


@dataclass
class LifecycleRecord:
    """Current state plus the bounded history that got it there."""

    state: str = AgentState.CREATED
    history: list = field(default_factory=list)
    validation: dict = field(default_factory=dict)
    benchmark: dict = field(default_factory=dict)

    MAX_HISTORY = 50

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "history": [item.to_dict() for item in self.history],
            "validation": dict(self.validation),
            "benchmark": dict(self.benchmark),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "LifecycleRecord":
        payload = payload or {}
        state = str(payload.get("state", AgentState.CREATED))
        if state not in AgentState.ALL:
            raise AgentLifecycleError("Unknown lifecycle state: %r" % state)
        history = [Transition.from_dict(item)
                   for item in (payload.get("history") or [])
                   if isinstance(item, dict)]
        return cls(state=state, history=history[-cls.MAX_HISTORY:],
                   validation=dict(payload.get("validation") or {}),
                   benchmark=dict(payload.get("benchmark") or {}))

    # -- queries ---------------------------------------------------------

    @property
    def runnable(self) -> bool:
        return self.state in AgentState.RUNNABLE

    def allowed(self) -> tuple:
        """States reachable from the current one (including a re-spec)."""
        reachable = list(TRANSITIONS.get(self.state, ()))
        if self.state != AgentState.RETIRED and RESPEC_TARGET not in reachable:
            reachable.append(RESPEC_TARGET)
        return tuple(reachable)

    def can(self, target: str) -> bool:
        return target in self.allowed()

    # -- mutation --------------------------------------------------------

    def respec(self, *, actor: str, reason: str, at: float = 0.0) -> Transition:
        """Record a specification change: evidence is invalidated.

        From any non-terminal state this returns the agent to ``created``.
        When it is *already* ``created`` the state cannot change, but the
        change is still recorded and any stale evidence is cleared — a
        spec edit must never fail just because there was nothing to
        invalidate yet.
        """
        if self.state == AgentState.RETIRED:
            raise AgentLifecycleError(
                "A retired agent cannot be re-specified")
        if self.state != RESPEC_TARGET:
            return self.transition(RESPEC_TARGET, actor=actor, reason=reason,
                                   at=at)
        record = Transition(from_state=self.state, to_state=RESPEC_TARGET,
                            actor=actor.strip(),
                            reason=(reason or "").strip(),
                            at=at or time.time())
        self.history.append(record)
        self.history = self.history[-self.MAX_HISTORY:]
        self.validation = {}
        self.benchmark = {}
        return record

    def transition(self, target: str, *, actor: str, reason: str,
                   at: float = 0.0) -> Transition:
        """Move to ``target`` or raise, recording the attempt only if legal."""
        if not actor or not actor.strip():
            raise AgentLifecycleError(
                "A lifecycle transition needs an actor")
        if target not in AgentState.ALL:
            raise AgentLifecycleError("Unknown lifecycle state: %r" % target)
        if target == self.state:
            raise AgentLifecycleError(
                "Agent is already %s" % self.state)
        if not self.can(target):
            raise AgentLifecycleError(
                "Illegal lifecycle transition %s -> %s (allowed: %s)"
                % (self.state, target, ", ".join(self.allowed()) or "none"))
        if target == AgentState.ENABLED and not self.benchmark.get("passed"):
            raise AgentLifecycleError(
                "Cannot enable without a passing benchmark report")
        record = Transition(from_state=self.state, to_state=target,
                            actor=actor.strip(), reason=(reason or "").strip(),
                            at=at or time.time())
        self.state = target
        self.history.append(record)
        self.history = self.history[-self.MAX_HISTORY:]
        if target == RESPEC_TARGET:
            # New spec: the recorded evidence describes the old one.
            self.validation = {}
            self.benchmark = {}
        return record
