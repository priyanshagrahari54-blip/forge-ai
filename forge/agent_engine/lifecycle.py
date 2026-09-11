"""Agent lifecycle state machine (A81).

Every created agent moves through an explicit, centrally enforced
lifecycle:

    created → validated → tested → enabled ⇄ paused
                                      ↓          ↓
                                    disabled → retired

- ``created``   the package exists; nothing has been checked yet.
- ``validated`` the specification and package integrity have passed.
- ``tested``    the required benchmark suite passed at the required score.
- ``enabled``   the agent may execute tasks (the only runnable state).
- ``paused``    execution is temporarily halted; resumable to ``enabled``.
- ``disabled``  execution is halted; re-entry requires re-validation and
                re-testing (never a direct jump back to ``enabled``).
- ``retired``   terminal state; nothing may run, ever again.

Only :class:`forge.agent_engine.manager.AgentManager` may perform
transitions — agents never hold a reference to it, so an agent can never
move itself (or anything else) through this state machine.
"""
from __future__ import annotations

from enum import Enum

from forge.agent_engine.errors import LifecycleError


class AgentLifecycle(str, Enum):
    CREATED = "created"
    VALIDATED = "validated"
    TESTED = "tested"
    ENABLED = "enabled"
    PAUSED = "paused"
    DISABLED = "disabled"
    RETIRED = "retired"


#: The only permitted transitions. Anything not listed here is refused.
ALLOWED_TRANSITIONS: dict[AgentLifecycle, frozenset[AgentLifecycle]] = {
    AgentLifecycle.CREATED: frozenset({
        AgentLifecycle.VALIDATED,
        AgentLifecycle.RETIRED,
    }),
    AgentLifecycle.VALIDATED: frozenset({
        AgentLifecycle.VALIDATED,
        AgentLifecycle.TESTED,
        AgentLifecycle.RETIRED,
    }),
    AgentLifecycle.TESTED: frozenset({
        AgentLifecycle.TESTED,
        AgentLifecycle.VALIDATED,   # a failed re-test demotes honestly
        AgentLifecycle.ENABLED,
        AgentLifecycle.RETIRED,
    }),
    AgentLifecycle.ENABLED: frozenset({
        AgentLifecycle.PAUSED,
        AgentLifecycle.DISABLED,
    }),
    AgentLifecycle.PAUSED: frozenset({
        AgentLifecycle.ENABLED,
        AgentLifecycle.DISABLED,
    }),
    AgentLifecycle.DISABLED: frozenset({
        AgentLifecycle.VALIDATED,   # re-entry path: validate → test → enable
        AgentLifecycle.RETIRED,
    }),
    AgentLifecycle.RETIRED: frozenset(),
}

#: States in which the agent may execute tasks.
RUNNABLE_STATES = frozenset({AgentLifecycle.ENABLED})


def parse_lifecycle(value: str) -> AgentLifecycle:
    try:
        return AgentLifecycle(value)
    except ValueError:
        raise LifecycleError(
            f"unknown lifecycle state {value!r}; expected one of "
            f"{', '.join(state.value for state in AgentLifecycle)}"
        ) from None


def can_transition(current: AgentLifecycle | str,
                   target: AgentLifecycle | str) -> bool:
    current_state = current if isinstance(current, AgentLifecycle) \
        else parse_lifecycle(current)
    target_state = target if isinstance(target, AgentLifecycle) \
        else parse_lifecycle(target)
    return target_state in ALLOWED_TRANSITIONS.get(current_state, frozenset())


def require_transition(current: AgentLifecycle | str,
                       target: AgentLifecycle | str) -> None:
    if not can_transition(current, target):
        raise LifecycleError(
            f"illegal lifecycle transition: "
            f"{getattr(current, 'value', current)!r} -> "
            f"{getattr(target, 'value', target)!r}")


def is_runnable(state: AgentLifecycle | str) -> bool:
    state_obj = state if isinstance(state, AgentLifecycle) \
        else parse_lifecycle(state)
    return state_obj in RUNNABLE_STATES
