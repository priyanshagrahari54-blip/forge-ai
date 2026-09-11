"""Agent lifecycle (A81): the state machine every created agent follows.

States::

    created → validated → tested → enabled ⇄ paused
                                        │
                 (any live state) ──────┴→ disabled → retired
                                              ↑        (terminal)

Rules:

* Only ``enabled`` agents may run tasks — every other state refuses.
* ``validated`` is reached only by passing deterministic specification
  validation; ``tested`` only by passing the agent benchmark suite;
  ``enabled``/``paused``/``disabled`` are operator decisions.
* ``disabled`` can return to ``enabled`` only after a fresh benchmark
  pass (re-test) — disabling is not a free re-entry.
* ``retired`` is terminal: no transitions out, ever.
* A spec update (new version) resets the package to ``created``; the
  agent must be re-validated, re-tested, and re-enabled before it can
  run again. Changed agents never inherit their old clearance.
"""
from __future__ import annotations

from enum import Enum


class LifecycleState(str, Enum):
    CREATED = "created"
    VALIDATED = "validated"
    TESTED = "tested"
    ENABLED = "enabled"
    PAUSED = "paused"
    DISABLED = "disabled"
    RETIRED = "retired"


#: The single state an agent can run tasks from.
RUNNABLE_STATE = LifecycleState.ENABLED

#: Explicit transition table. Anything not listed is refused.
TRANSITIONS = {
    LifecycleState.CREATED: {
        LifecycleState.VALIDATED,   # deterministic spec validation passed
        LifecycleState.RETIRED,     # operator abandons before validating
    },
    LifecycleState.VALIDATED: {
        LifecycleState.TESTED,      # benchmark suite passed
        LifecycleState.RETIRED,
    },
    LifecycleState.TESTED: {
        LifecycleState.ENABLED,     # operator enables
        LifecycleState.RETIRED,
    },
    LifecycleState.ENABLED: {
        LifecycleState.PAUSED,      # operator pauses
        LifecycleState.DISABLED,    # operator disables
        LifecycleState.RETIRED,
    },
    LifecycleState.PAUSED: {
        LifecycleState.ENABLED,     # operator resumes
        LifecycleState.DISABLED,
        LifecycleState.RETIRED,
    },
    LifecycleState.DISABLED: {
        LifecycleState.TESTED,      # re-test after disable, then re-enable
        LifecycleState.RETIRED,
    },
    LifecycleState.RETIRED: set(),  # terminal
}

#: Transitions only an operator (never an agent) may drive.
OPERATOR_ONLY_TRANSITIONS = {
    (LifecycleState.TESTED, LifecycleState.ENABLED),
    (LifecycleState.ENABLED, LifecycleState.PAUSED),
    (LifecycleState.PAUSED, LifecycleState.ENABLED),
    (LifecycleState.ENABLED, LifecycleState.DISABLED),
    (LifecycleState.PAUSED, LifecycleState.DISABLED),
    (LifecycleState.DISABLED, LifecycleState.TESTED),
    (LifecycleState.CREATED, LifecycleState.RETIRED),
    (LifecycleState.VALIDATED, LifecycleState.RETIRED),
    (LifecycleState.TESTED, LifecycleState.RETIRED),
    (LifecycleState.ENABLED, LifecycleState.RETIRED),
    (LifecycleState.PAUSED, LifecycleState.RETIRED),
    (LifecycleState.DISABLED, LifecycleState.RETIRED),
}


class LifecycleError(ValueError):
    """Raised when a lifecycle transition is not allowed."""


def transition(state: LifecycleState | str,
               target: LifecycleState | str) -> LifecycleState:
    """Validate a transition and return the new state.

    Raises :class:`LifecycleError` with an honest explanation when the
    transition is not in the table, when the target is unknown, or when
    the agent is retired (terminal).
    """
    try:
        current = LifecycleState(state)
    except ValueError:
        raise LifecycleError(f"Unknown state {state!r}") from None
    try:
        goal = LifecycleState(target)
    except ValueError:
        raise LifecycleError(
            f"Unknown target state {target!r}; expected one of "
            + ", ".join(item.value for item in LifecycleState)) from None
    if current is LifecycleState.RETIRED:
        raise LifecycleError(
            "Retired agents are terminal; they can never transition again")
    if goal not in TRANSITIONS[current]:
        allowed = ", ".join(item.value
                            for item in sorted(TRANSITIONS[current],
                                               key=lambda s: s.value))
        raise LifecycleError(
            f"Cannot move an agent from {current.value!r} to "
            f"{goal.value!r}; allowed: {allowed or 'nothing (terminal)'}")
    return goal


def can_run(state: LifecycleState | str) -> bool:
    """True only for the enabled state — the single runnable state."""
    try:
        return LifecycleState(state) is RUNNABLE_STATE
    except ValueError:
        return False


def runnable_state() -> str:
    return RUNNABLE_STATE.value
