"""Agent lifecycle (Forge Agent Creation Engine).

Seven explicit states::

    created -> validated -> tested -> enabled
                                     enabled <-> paused
                                     enabled/paused/tested/... -> disabled
                                     * -> retired (terminal)

Only ``enabled`` agents may run. ``retired`` is terminal: a retired
agent can never transition again. Every transition is validated —
unknown states and illegal jumps raise instead of silently landing.
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


#: Legal outgoing transitions per state. ``retired`` has none: it is
#: terminal by construction.
ALLOWED_TRANSITIONS: dict[str, frozenset] = {
    LifecycleState.CREATED.value: frozenset({
        LifecycleState.VALIDATED.value,
        LifecycleState.DISABLED.value,
        LifecycleState.RETIRED.value,
    }),
    LifecycleState.VALIDATED.value: frozenset({
        LifecycleState.TESTED.value,
        LifecycleState.DISABLED.value,
        LifecycleState.RETIRED.value,
    }),
    LifecycleState.TESTED.value: frozenset({
        LifecycleState.ENABLED.value,
        LifecycleState.DISABLED.value,
        LifecycleState.RETIRED.value,
    }),
    LifecycleState.ENABLED.value: frozenset({
        LifecycleState.PAUSED.value,
        LifecycleState.DISABLED.value,
        LifecycleState.RETIRED.value,
    }),
    LifecycleState.PAUSED.value: frozenset({
        LifecycleState.ENABLED.value,
        LifecycleState.DISABLED.value,
        LifecycleState.RETIRED.value,
    }),
    LifecycleState.DISABLED.value: frozenset({
        LifecycleState.ENABLED.value,
        LifecycleState.RETIRED.value,
    }),
    LifecycleState.RETIRED.value: frozenset(),
}

#: States an agent must be in before it may be enabled. ``created``
#: and ``validated`` are deliberately absent: enabling requires a
#: passing benchmark (``tested``) or a previous enable (``paused`` /
#: ``disabled``).
ENABLE_FROM = frozenset({
    LifecycleState.TESTED.value,
    LifecycleState.PAUSED.value,
    LifecycleState.DISABLED.value,
})


def normalize(state: str) -> str:
    """Return the canonical lifecycle value or raise for unknown ones."""
    candidate = (state or "").strip().lower()
    for known in LifecycleState:
        if candidate == known.value:
            return candidate
    raise ValueError(
        "Unknown lifecycle state %r; expected one of: %s"
        % (state, ", ".join(sorted(ALLOWED_TRANSITIONS))))


def can_transition(frm: str, to: str) -> bool:
    """True when ``frm -> to`` is a legal lifecycle transition."""
    try:
        source = normalize(frm)
        target = normalize(to)
    except ValueError:
        return False
    return target in ALLOWED_TRANSITIONS[source]


def check_transition(frm: str, to: str) -> tuple[str, str]:
    """Validate ``frm -> to``; return the normalized pair or raise."""
    source = normalize(frm)
    target = normalize(to)
    if target not in ALLOWED_TRANSITIONS[source]:
        raise ValueError(
            "Illegal lifecycle transition: %s -> %s" % (source, target))
    return source, target


def is_runnable(state: str) -> bool:
    """Only ``enabled`` agents may run. Everything else refuses."""
    return normalize(state) == LifecycleState.ENABLED.value
