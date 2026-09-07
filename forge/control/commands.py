"""Finite, safe command vocabulary for the cockpit (A34).

Natural language and voice input NEVER become shell commands: they are
translated here — deterministically, with no model in the loop — into one
of the finite :class:`ControlCommand` values (or
``REQUIRE_CLARIFICATION`` / ``DENY``). Execution happens only in the
control plane, authorized and audited like any other API call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ControlCommand(str, Enum):
    START_TASK = "START_TASK"
    PAUSE_TASK = "PAUSE_TASK"
    RESUME_TASK = "RESUME_TASK"
    CANCEL_TASK = "CANCEL_TASK"
    RETRY_TASK = "RETRY_TASK"
    APPROVE = "APPROVE"
    DENY = "DENY"
    ROLLBACK = "ROLLBACK"


class InterpretedKind(str, Enum):
    COMMAND = "COMMAND"
    REQUIRE_CLARIFICATION = "REQUIRE_CLARIFICATION"
    DENY = "DENY"


@dataclass(frozen=True)
class Interpretation:
    kind: InterpretedKind
    command: ControlCommand | None = None
    slots: dict[str, str] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "command": self.command.value if self.command else None,
            "slots": dict(self.slots),
            "reason": self.reason,
        }


#: Patterns that can never become commands: anything resembling a shell
#: invocation, path escape, or(values are matched case-insensitively).
_DENIED_PATTERNS = (
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r";\s*\w"),
    re.compile(r"\|\s*\w"),
    re.compile(r"`[^`]*`"),
    re.compile(r"\$\("),
    re.compile(r"\.\./"),
    re.compile(r"\bgit\s+(reset|push|add\s+\.|add\s+-A)\b"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bchmod\b"),
    re.compile(r"\bshutdown\b|\breboot\b"),
    re.compile(r"\bexec\b|\beval\b"),
)

#: (pattern, command, needs_slot) — first match wins. Slots are extracted
#: as opaque ids that the control plane validates against real state.
_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], ControlCommand, str], ...] = (
    (re.compile(r"\b(pause|hold|suspend)\b.*\btask\b"), ControlCommand.PAUSE_TASK, "task"),
    (re.compile(r"\btask\b.*\b(pause|hold|suspend)\b"), ControlCommand.PAUSE_TASK, "task"),
    (re.compile(r"\b(resume|continue|unpause)\b.*\btask\b"), ControlCommand.RESUME_TASK, "task"),
    (re.compile(r"\btask\b.*\b(resume|continue|unpause)\b"), ControlCommand.RESUME_TASK, "task"),
    (re.compile(r"\b(cancel|stop|abort|kill)\b.*\btask\b"), ControlCommand.CANCEL_TASK, "task"),
    (re.compile(r"\btask\b.*\b(cancel|stop|abort|kill)\b"), ControlCommand.CANCEL_TASK, "task"),
    (re.compile(r"\b(retry|restart|rerun|run again)\b.*\btask\b"), ControlCommand.RETRY_TASK, "task"),
    (re.compile(r"\brollback\b.*\btask\b"), ControlCommand.ROLLBACK, "task"),
    (re.compile(r"\btask\b.*\brollback\b"), ControlCommand.ROLLBACK, "task"),
    (re.compile(r"\b(start|create|run|launch|begin)\b.*\btask\b"), ControlCommand.START_TASK, "requirement"),
    (re.compile(r"\bapprove\b"), ControlCommand.APPROVE, "approval"),
    (re.compile(r"\ballow\b"), ControlCommand.APPROVE, "approval"),
    (re.compile(r"\bdeny\b|\breject\b|\brefuse\b"), ControlCommand.DENY, "approval"),
)

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,127}")


def interpret_text(text: str, *, active_task: str = "",
                   pending_approval: str = "") -> Interpretation:
    """Translate free text into a finite command. Never executes anything."""
    if not isinstance(text, str) or not text.strip():
        return Interpretation(InterpretedKind.REQUIRE_CLARIFICATION,
                              reason="Empty command.")
    cleaned = text.strip()
    if len(cleaned) > 2000:
        return Interpretation(InterpretedKind.DENY,
                              reason="Command text too long.")
    lowered = cleaned.lower()
    for denied in _DENIED_PATTERNS:
        if denied.search(lowered):
            return Interpretation(
                InterpretedKind.DENY,
                reason="That looks like a shell/system command, which the "
                       "cockpit never executes. Use a task command instead.")
    for pattern, command, slot in _COMMAND_PATTERNS:
        if not pattern.search(lowered):
            continue
        if command == ControlCommand.START_TASK:
            # The requirement is the full text minus the trigger verb; it
            # becomes a task description, never code or a command.
            requirement = pattern.sub("", cleaned, count=1).strip(" :-.\"'")
            if len(requirement) < 8:
                return Interpretation(
                    InterpretedKind.REQUIRE_CLARIFICATION,
                    reason="What should the task do? Describe the requirement.")
            return Interpretation(InterpretedKind.COMMAND, command,
                                  {"requirement": requirement[:2000]})
        if slot == "task":
            task_id = _extract_id(cleaned) or active_task
            if not task_id:
                return Interpretation(
                    InterpretedKind.REQUIRE_CLARIFICATION, command,
                    reason="Which task? Provide a task id.")
            return Interpretation(InterpretedKind.COMMAND, command,
                                  {"task_id": task_id})
        approval_id = _extract_id(cleaned) or pending_approval
        if not approval_id:
            return Interpretation(
                InterpretedKind.REQUIRE_CLARIFICATION, command,
                reason="Which approval request? Provide an approval id.")
        return Interpretation(InterpretedKind.COMMAND, command,
                              {"approval_id": approval_id})
    return Interpretation(
        InterpretedKind.REQUIRE_CLARIFICATION,
        reason="I only understand task commands: start, pause, resume, "
               "cancel, retry, rollback, approve, deny.")


def interpret_voice_intent(intent_name: str, slots: dict[str, str],
                           *, active_task: str = "",
                           pending_approval: str = "") -> Interpretation:
    """Map a parsed :mod:`forge.voice` intent onto the finite vocabulary."""
    mapping = {
        "start_task": (ControlCommand.START_TASK, "requirement"),
        "pause_task": (ControlCommand.PAUSE_TASK, "task"),
        "resume_task": (ControlCommand.RESUME_TASK, "task"),
        "cancel_task": (ControlCommand.CANCEL_TASK, "task"),
        "retry_task": (ControlCommand.RETRY_TASK, "task"),
        "approve": (ControlCommand.APPROVE, "approval"),
        "deny": (ControlCommand.DENY, "approval"),
        "rollback": (ControlCommand.ROLLBACK, "task"),
    }
    entry = mapping.get((intent_name or "").lower())
    if entry is None:
        return Interpretation(
            InterpretedKind.REQUIRE_CLARIFICATION,
            reason=f"Unknown voice intent {intent_name!r}.")
    command, slot = entry
    if slot == "requirement":
        requirement = str(slots.get("requirement", "") or "").strip()
        if len(requirement) < 8:
            return Interpretation(
                InterpretedKind.REQUIRE_CLARIFICATION, command,
                reason="What should the task do?")
        return Interpretation(InterpretedKind.COMMAND, command,
                              {"requirement": requirement[:2000]})
    if slot == "task":
        task_id = str(slots.get("task_id", "") or active_task).strip()
        if not task_id:
            return Interpretation(
                InterpretedKind.REQUIRE_CLARIFICATION, command,
                reason="Which task?")
        return Interpretation(InterpretedKind.COMMAND, command,
                              {"task_id": task_id})
    approval_id = str(slots.get("approval_id", "") or pending_approval).strip()
    if not approval_id:
        return Interpretation(
            InterpretedKind.REQUIRE_CLARIFICATION, command,
            reason="Which approval request?")
    return Interpretation(InterpretedKind.COMMAND, command,
                          {"approval_id": approval_id})


def _extract_id(text: str) -> str:
    """Extract an opaque id token (validated later against real state)."""
    candidates = _ID_RE.findall(text)
    # Prefer long hex-looking tokens (task/approval ids); ignore plain words
    # like "task", "the", "please".
    for candidate in sorted(candidates, key=len, reverse=True):
        if len(candidate) >= 8 and candidate.lower() not in {
                "task", "please", "approval", "request"}:
            return candidate
    return ""
