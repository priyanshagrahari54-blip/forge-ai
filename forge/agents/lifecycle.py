"""Agent lifecycle for the first-party Creation Engine.

Seven explicit states gate every managed agent::

    created -> validated -> tested -> enabled <-> paused
                                      enabled -> disabled -> enabled
                                      * -> retired (terminal)

- ``created`` — spec accepted, not yet validated.
- ``validated`` — spec validation passed.
- ``tested`` — benchmark suite passed.
- ``enabled`` — may run tasks (and only in this state).
- ``paused`` — temporarily suspended; may resume to ``enabled``.
- ``disabled`` — administratively disabled; may be re-enabled.
- ``retired`` — terminal; no further transitions.

Every transition is explicit and audited by the caller. Unknown states
and skipped steps are refused — an agent can never jump from
``created`` to ``enabled`` without passing validation and testing.
"""
from __future__ import annotations

CREATED = "created"
VALIDATED = "validated"
TESTED = "tested"
ENABLED = "enabled"
PAUSED = "paused"
DISABLED = "disabled"
RETIRED = "retired"

ALL_STATES: tuple[str, ...] = (
    CREATED, VALIDATED, TESTED, ENABLED, PAUSED, DISABLED, RETIRED,
)
ALL_STATES_SET = frozenset(ALL_STATES)

#: Allowed next states per current state.
TRANSITIONS: dict[str, frozenset[str]] = {
    CREATED: frozenset({VALIDATED, RETIRED}),
    VALIDATED: frozenset({TESTED, CREATED, RETIRED}),
    TESTED: frozenset({ENABLED, VALIDATED, CREATED, RETIRED}),
    ENABLED: frozenset({PAUSED, DISABLED, RETIRED}),
    PAUSED: frozenset({ENABLED, DISABLED, RETIRED}),
    DISABLED: frozenset({ENABLED, RETIRED}),
    RETIRED: frozenset(),
}

#: Human-readable reason recorded for each transition kind.
TRANSITION_REASONS: dict[tuple[str, str], str] = {
    (CREATED, VALIDATED): "spec validation passed",
    (VALIDATED, TESTED): "benchmark suite passed",
    (TESTED, ENABLED): "operator enabled after testing",
    (ENABLED, PAUSED): "operator paused",
    (PAUSED, ENABLED): "operator resumed",
    (ENABLED, DISABLED): "operator disabled",
    (PAUSED, DISABLED): "operator disabled",
    (DISABLED, ENABLED): "operator re-enabled",
    (VALIDATED, CREATED): "spec changed; re-validation required",
    (TESTED, CREATED): "spec changed; re-validation required",
    (TESTED, VALIDATED): "re-testing required",
}


def is_state(value: object) -> bool:
    return isinstance(value, str) and value in ALL_STATES_SET


def can_transition(current: str, target: str) -> bool:
    if current not in ALL_STATES_SET or target not in ALL_STATES_SET:
        return False
    return target in TRANSITIONS[current]


def check_transition(current: str, target: str) -> None:
    """Raise ``ValueError`` unless ``current -> target`` is legal."""
    if current not in ALL_STATES_SET:
        raise ValueError(f"Unknown lifecycle state {current!r}")
    if target not in ALL_STATES_SET:
        raise ValueError(f"Unknown lifecycle state {target!r}")
    if target not in TRANSITIONS[current]:
        if current == RETIRED:
            raise ValueError("Retired agents cannot transition")
        raise ValueError(
            f"Illegal lifecycle transition {current!r} -> {target!r}; "
            f"allowed: {sorted(TRANSITIONS[current]) or ['none']}")


def reason_for(current: str, target: str) -> str:
    if target == RETIRED:
        return f"operator retired from {current}"
    return TRANSITION_REASONS.get(
        (current, target), f"{current} -> {target}")
