"""Computer-use state: versioned screen snapshots and redacted action
history (A40).

Everything is bounded and redacted by construction:

* snapshots are versioned per task and capped (oldest evicted);
* action history stores redacted parameters only — typed text is
  replaced with a length-only placeholder the moment it is recorded,
  so logs, cockpit views, and memory can never leak what was typed;
* the confirmation-dialog flag is derived from the latest screen
  understanding and drives the fail-closed execution guard.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

MAX_SNAPSHOTS_PER_TASK = 8
MAX_ACTIONS_PER_TASK = 40
MAX_TASKS = 64

#: Kinds whose parameters may carry typed text.
TEXT_BEARING_KINDS = {"keyboard", "clipboard", "app_action", "file_select",
                      "file_access", "launch"}


SENSITIVE_KEY_MARKERS = ("secret", "pass", "token", "credential",
                          "auth", "key")


def _sensitive_key(key: str) -> bool:
    lowered = str(key).lower()
    if lowered in ("text", "value", "content", "path", "command", "url"):
        return True
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def redact_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``params`` safe for history/logs/memory.

    Typed text, paths, commands, URLs, and any secret-bearing key are
    replaced with a length-only placeholder; nested dicts recurse.
    """
    out: dict[str, Any] = {}
    for key, value in params.items():
        if _sensitive_key(key) or (key == "keys" and kind == "keyboard"):
            out[key] = f"<redacted: {len(str(value))} chars>"
        elif isinstance(value, dict):
            out[key] = redact_params(kind, value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class ScreenSnapshot:
    version: int
    sha256: str
    format: str
    width: int | None
    height: int | None
    taken_at: float
    goal: str = ""
    understanding: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "taken_at": self.taken_at,
            "goal": self.goal,
            "summary": self.understanding.get("summary", ""),
        }


@dataclass
class ComputerState:
    task_id: str
    snapshots: deque = field(default_factory=lambda: deque(
        maxlen=MAX_SNAPSHOTS_PER_TASK))
    actions: deque = field(default_factory=lambda: deque(
        maxlen=MAX_ACTIONS_PER_TASK))
    executed_actions: int = 0
    confirm_dialog: bool = False

    def snapshot_versions(self) -> list[int]:
        return [snapshot.version for snapshot in self.snapshots]

    def latest_snapshot(self) -> ScreenSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "snapshots": [snapshot.to_dict() for snapshot in self.snapshots],
            "actions": list(self.actions),
            "executed_actions": self.executed_actions,
            "confirm_dialog": self.confirm_dialog,
        }


class ComputerStateStore:
    """Bounded per-task state; oldest tasks evicted first."""

    def __init__(self, max_tasks: int = MAX_TASKS) -> None:
        self.max_tasks = max_tasks
        self._states: OrderedDict[str, ComputerState] = OrderedDict()

    def state(self, task_id: str) -> ComputerState:
        state = self._states.get(task_id)
        if state is None:
            state = ComputerState(task_id=task_id)
            self._states[task_id] = state
            self._states.move_to_end(task_id)
            while len(self._states) > self.max_tasks:
                self._states.popitem(last=False)
        else:
            self._states.move_to_end(task_id)
        return state

    def record_snapshot(self, task_id: str, image: bytes, *, goal: str,
                        understanding: dict[str, Any]) -> ScreenSnapshot:
        state = self.state(task_id)
        version = (state.snapshots[-1].version + 1
                   if state.snapshots else 1)
        snapshot = ScreenSnapshot(
            version=version,
            sha256=hashlib.sha256(image).hexdigest()[:16],
            format=understanding.get("format", "unknown"),
            width=understanding.get("width"),
            height=understanding.get("height"),
            taken_at=time.time(),
            goal=goal,
            understanding=understanding,
        )
        state.snapshots.append(snapshot)
        state.confirm_dialog = detect_confirm_dialog(understanding)
        return snapshot

    def record_action(self, task_id: str, action: dict[str, Any]) -> None:
        state = self.state(task_id)
        if action.get("executed"):
            state.executed_actions += 1
        state.actions.append(action)

    def history(self, task_id: str) -> dict[str, Any]:
        return self.state(task_id).to_dict()


def detect_confirm_dialog(understanding: dict[str, Any]) -> bool:
    """Fail-closed detection of confirmation dialogs on screen.

    Real markers found in the screen's own extracted text make any
    subsequent actuation refuse until a fresh screen without a dialog
    is observed.
    """
    markers = ("are you sure", "confirm", "proceed?", "are you certain",
               "yes/no", "ok/cancel", "delete this", "overwrite")
    lowered = " ".join(
        str(finding.get("content", ""))
        for finding in understanding.get("findings", [])
    ).lower()
    return any(marker in lowered for marker in markers)
