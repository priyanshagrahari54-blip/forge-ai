"""Agent lifecycle (A81): explicit, validated state machine.

States:

``created``    the package exists but nothing has been checked
``validated``  the specification and package passed structural validation
``tested``     the benchmark suite ran and met the pass threshold
``enabled``    the agent may run (only reachable from ``tested``)
``paused``     temporarily stopped; can be re-enabled without retesting
``disabled``   stopped by an operator; must be retested before enabling
``retired``    terminal; no transitions out, ever

Only an *operator actor* may move an agent forward into ``enabled``.
An agent can never transition itself: :meth:`LifecycleManager.transition`
refuses when the actor is the agent itself.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

CREATED = "created"
VALIDATED = "validated"
TESTED = "tested"
ENABLED = "enabled"
PAUSED = "paused"
DISABLED = "disabled"
RETIRED = "retired"

STATES = (CREATED, VALIDATED, TESTED, ENABLED, PAUSED, DISABLED, RETIRED)

#: Allowed transitions. Anything not listed is refused.
TRANSITIONS = {
    CREATED: (VALIDATED, DISABLED, RETIRED),
    VALIDATED: (TESTED, DISABLED, RETIRED),
    TESTED: (ENABLED, VALIDATED, DISABLED, RETIRED),
    ENABLED: (PAUSED, DISABLED, RETIRED),
    PAUSED: (ENABLED, DISABLED, RETIRED),
    DISABLED: (VALIDATED, RETIRED),
    RETIRED: (),
}

#: States in which an agent may actually execute work.
RUNNABLE_STATES = frozenset({ENABLED})

#: Transitions only a human/operator actor may perform.
OPERATOR_ONLY = frozenset({ENABLED, RETIRED})


class LifecycleError(RuntimeError):
    """An illegal or unauthorized lifecycle transition was requested."""


@dataclass
class LifecycleEvent:
    agent: str
    from_state: str
    to_state: str
    actor: str
    reason: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"agent": self.agent, "from": self.from_state,
                "to": self.to_state, "actor": self.actor,
                "reason": self.reason, "at": self.at}


class LifecycleManager:
    """Validated lifecycle transitions with a full audit history."""

    def __init__(self) -> None:
        self._states: dict = {}
        self._history: list = []

    def register(self, agent: str, state: str = CREATED) -> str:
        if state not in STATES:
            raise LifecycleError("Unknown state: {0!r}".format(state))
        self._states[agent] = state
        self._history.append(LifecycleEvent(agent, "", state, "system",
                                            "registered"))
        return state

    def state(self, agent: str) -> str:
        if agent not in self._states:
            raise LifecycleError("Unknown agent: {0!r}".format(agent))
        return self._states[agent]

    def can_run(self, agent: str) -> bool:
        return self._states.get(agent, "") in RUNNABLE_STATES

    def transition(self, agent: str, to_state: str, *, actor: str,
                   reason: str = "") -> dict:
        current = self.state(agent)
        actor = (actor or "").strip()
        if not actor:
            raise LifecycleError("A lifecycle transition needs an actor")
        if to_state not in STATES:
            raise LifecycleError("Unknown state: {0!r}".format(to_state))
        if current == RETIRED:
            raise LifecycleError(
                "{0!r} is retired; retirement is terminal".format(agent))
        if to_state not in TRANSITIONS[current]:
            raise LifecycleError(
                "Illegal transition {0} -> {1} for {2!r}".format(
                    current, to_state, agent))
        if actor == agent:
            raise LifecycleError(
                "An agent may not change its own lifecycle state")
        if to_state in OPERATOR_ONLY and actor.startswith("agent:"):
            raise LifecycleError(
                "Only an operator may move {0!r} to {1}".format(
                    agent, to_state))
        self._states[agent] = to_state
        event = LifecycleEvent(agent, current, to_state, actor, reason)
        self._history.append(event)
        return event.to_dict()

    def history(self, agent: str = "") -> list:
        return [event.to_dict() for event in self._history
                if not agent or event.agent == agent]

    def snapshot(self) -> dict:
        return dict(sorted(self._states.items()))
