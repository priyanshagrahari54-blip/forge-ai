"""Live engine state + the durable status snapshot for UI surfaces.

The engine mutates a :class:`StateTracker`; after every stage transition the
tracker writes an atomic JSON snapshot to ``<root>/.forge/native/state.json``
(temp file + ``os.replace`` — safe on Windows, where renaming over an existing
file needs the replace semantics). The desktop Native AI panel, the
``forge native-ai`` CLI, and the cockpit poll *that file*, so status works
across processes without a server or sockets — deliberately cheap on a 2 GB
machine.

The snapshot reports exactly what requirement A81 lists: engine state, active
reasoning backend, model backend, task state, current stage, verification
state, and retry state. Every field is real: state names come from the
running engine, verification/retry entries are recorded by the actual gates,
and absence is encoded as ``None``/empty — never as optimistic defaults.
"""
from __future__ import annotations

import json
import os
from time import time_ns
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

#: Bump when the snapshot schema changes so readers can branch.
SNAPSHOT_VERSION = 1

#: Snapshot file location relative to the project root.
SNAPSHOT_RELATIVE_PATH = os.path.join(".forge", "native", "state.json")

#: A snapshot newer than this many seconds is "fresh" for UI purposes. The
#: desktop panel treats older snapshots as historical, never as live state.
FRESHNESS_SECONDS = 120.0


class EngineState(str, Enum):
    """Where the engine is in its lifecycle. Terminal states are final."""

    IDLE = "idle"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    INSPECTING = "inspecting"
    CONTEXT = "context"
    REASONING = "reasoning"
    CODING = "coding"
    TESTING = "testing"
    DEBUGGING = "debugging"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    MEMORIZING = "memorizing"
    REPORTING = "reporting"
    # Terminal states:
    COMPLETED = "completed"
    PARTIAL = "partial"            # did real work, but neural-only steps skipped
    NEEDS_MODEL = "needs_model"    # an edit-class plan cannot proceed without inference
    BLOCKED_APPROVAL = "blocked"   # stopped at a policy gate; never bypassed
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset({
    EngineState.COMPLETED,
    EngineState.PARTIAL,
    EngineState.NEEDS_MODEL,
    EngineState.BLOCKED_APPROVAL,
    EngineState.FAILED,
    EngineState.CANCELLED,
})


class StageKind(str, Enum):
    """Planner step kinds (A81 layer 2). Also used as the run's stage axis."""

    INSPECT = "inspect"
    REASON = "reason"
    EDIT = "edit"
    TEST = "test"
    DEBUG = "debug"
    REVIEW = "review"
    FINISH = "finish"

    @classmethod
    def ordered(cls) -> "tuple[StageKind, ...]":
        return (cls.INSPECT, cls.REASON, cls.EDIT, cls.TEST, cls.DEBUG,
                cls.REVIEW, cls.FINISH)


class TaskState(str, Enum):
    """State of the current task from the outside (UI) point of view."""

    NONE = "none"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_MODEL = "needs_model"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class VerificationStatus:
    """Aggregated verification state carried into the snapshot."""

    status: str = "PENDING"  # PENDING | PASS | FAIL | PARTIAL
    executed: int = 0
    passed: int = 0
    failed: list = field(default_factory=list)      # names of failed gates
    skipped: list = field(default_factory=list)      # gates not executed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "executed": self.executed,
            "passed": self.passed,
            "failed": list(self.failed),
            "skipped": list(self.skipped),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "VerificationStatus":
        data = dict(data or {})
        return cls(
            status=str(data.get("status", "PENDING")),
            executed=int(data.get("executed", 0)),
            passed=int(data.get("passed", 0)),
            failed=[str(name) for name in data.get("failed", [])],
            skipped=[str(name) for name in data.get("skipped", [])],
        )


@dataclass
class NativeStatusSnapshot:
    """Serializable, self-describing engine status."""

    engine_state: str = EngineState.IDLE.value
    project: str = ""
    root: str = ""
    task_text: str = ""
    task_id: str = ""
    task_state: str = TaskState.NONE.value
    stage: str = ""
    stage_index: int = 0
    stage_total: int = 0
    reasoning_backend: Dict[str, Any] = field(default_factory=dict)
    model_backend: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=VerificationStatus().to_dict)
    retry: Dict[str, Any] = field(default_factory=lambda: {
        "cycle": 0, "max": 0, "active": False, "last_reason": "",
    })
    files_changed: list = field(default_factory=list)
    error: str = ""
    updated_at: float = 0.0
    pid: int = 0
    run_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": SNAPSHOT_VERSION,
            "engine_state": self.engine_state,
            "project": self.project,
            "root": self.root,
            "task": {"text": self.task_text[:400], "id": self.task_id,
                     "state": self.task_state},
            "stage": {"kind": self.stage, "index": self.stage_index,
                      "total": self.stage_total},
            "reasoning_backend": dict(self.reasoning_backend),
            "model_backend": dict(self.model_backend),
            "verification": dict(self.verification),
            "retry": dict(self.retry),
            "files_changed": list(self.files_changed),
            "error": self.error,
            "updated_at": self.updated_at,
            "pid": self.pid,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "NativeStatusSnapshot":
        data = dict(data or {})
        task = dict(data.get("task") or {})
        stage = dict(data.get("stage") or {})
        return cls(
            engine_state=str(data.get("engine_state", EngineState.IDLE.value)),
            project=str(data.get("project", "")),
            root=str(data.get("root", "")),
            task_text=str(task.get("text", "")),
            task_id=str(task.get("id", "")),
            task_state=str(task.get("state", TaskState.NONE.value)),
            stage=str(stage.get("kind", "")),
            stage_index=int(stage.get("index", 0)),
            stage_total=int(stage.get("total", 0)),
            reasoning_backend=dict(data.get("reasoning_backend") or {}),
            model_backend=dict(data.get("model_backend") or {}),
            verification=dict(data.get("verification")
                              or VerificationStatus().to_dict()),
            retry=dict(data.get("retry")
                       or {"cycle": 0, "max": 0, "active": False,
                           "last_reason": ""}),
            files_changed=list(data.get("files_changed") or []),
            error=str(data.get("error", "")),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
            pid=int(data.get("pid", 0) or 0),
            run_id=str(data.get("run_id", "")),
        )


class StateTracker:
    """Thread-safe tracker that mirrors engine state to ``state.json``.

    ``persist=False`` keeps everything in memory (tests, headless one-shot
    runs). Writing is best-effort by design: status observability must never
    crash a run that is otherwise healthy, but a write failure is recorded in
    ``last_persist_error`` and surfaced in reports so it can never hide.
    """

    def __init__(self, root: str | Path = ".", persist: bool = True,
                 project: str = "forge-ai") -> None:
        self.root = Path(root).resolve()
        self.persist = persist
        self._lock = threading.Lock()
        self._stage_started = time.monotonic()
        self.snapshot = NativeStatusSnapshot(
            project=project, root=str(self.root), pid=os.getpid())

    # -- mutation surface used by the engine ---------------------------------

    def snapshot_path(self) -> Path:
        return self.root / Path(SNAPSHOT_RELATIVE_PATH)

    def set_state(self, state: EngineState, error: str = "") -> None:
        with self._lock:
            self.snapshot.engine_state = EngineState(state).value
            self.snapshot.error = error
            self._touch_and_persist_locked()

    def start_task(self, task_id: str, task_text: str) -> None:
        with self._lock:
            self.snapshot.task_id = task_id
            self.snapshot.task_text = task_text
            self.snapshot.task_state = TaskState.RUNNING.value
            self.snapshot.files_changed = []
            self.snapshot.error = ""
            self.snapshot.verification = VerificationStatus().to_dict()
            self.snapshot.retry = {"cycle": 0, "max": self.snapshot.retry.get(
                "max", 0), "active": False, "last_reason": ""}
            self._touch_and_persist_locked()

    def end_task(self, task_state: TaskState, error: str = "") -> None:
        with self._lock:
            self.snapshot.task_state = TaskState(task_state).value
            self._touch_and_persist_locked(error)

    def set_stage(self, stage: StageKind, index: int, total: int) -> None:
        with self._lock:
            self._stage_started = time.monotonic()
            self.snapshot.stage = StageKind(stage).value
            self.snapshot.stage_index = int(index)
            self.snapshot.stage_total = int(total)
            self._touch_and_persist_locked()

    def set_backends(self, reasoning: Dict[str, Any],
                     model: Dict[str, Any]) -> None:
        with self._lock:
            self.snapshot.reasoning_backend = dict(reasoning)
            self.snapshot.model_backend = dict(model)
            self._touch_and_persist_locked()

    def set_retry(self, cycle: int, maximum: int, active: bool,
                   last_reason: str = "") -> None:
        with self._lock:
            self.snapshot.retry = {"cycle": int(cycle), "max": int(maximum),
                                   "active": bool(active),
                                   "last_reason": last_reason}
            self._touch_and_persist_locked()

    def set_verification(self, status: VerificationStatus) -> None:
        with self._lock:
            self.snapshot.verification = status.to_dict()
            self._touch_and_persist_locked()

    def record_files_changed(self, paths: list) -> None:
        with self._lock:
            merged = list(dict.fromkeys(list(self.snapshot.files_changed)
                                        + [str(p) for p in paths]))
            self.snapshot.files_changed = merged
            self._touch_and_persist_locked()

    def set_run_id(self, run_id: str) -> None:
        with self._lock:
            self.snapshot.run_id = run_id
            self._touch_and_persist_locked()

    @property
    def stage_elapsed(self) -> float:
        return time.monotonic() - self._stage_started

    # -- persistence ----------------------------------------------------------

    def _touch_and_persist_locked(self, error: str = "") -> None:
        if error:
            self.snapshot.error = error
        self.snapshot.updated_at = time.time()
        self.snapshot.pid = os.getpid()
        if self.persist:
            try:
                write_snapshot(self.root, self.snapshot)
                self.last_persist_error = ""
            except OSError as exc:  # observability must never break a run
                self.last_persist_error = str(exc)

    last_persist_error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return self.snapshot.to_dict()


def snapshot_dir(root: str | Path) -> Path:
    return Path(root) / ".forge" / "native"


def write_snapshot(root: str | Path,
                   snapshot: NativeStatusSnapshot) -> Path:
    """Atomically persist ``snapshot`` under ``<root>/.forge/native``.

    The temp name is unique per writer: a fixed ``.tmp`` name would let two
    engine processes interleave writes into the same temp file before either
    rename, publishing a corrupted snapshot.
    """
    path = Path(root) / Path(SNAPSHOT_RELATIVE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot.to_dict(), indent=2, sort_keys=True,
                         default=str)
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(),
                                           time_ns() % 1_000_000))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return path


def read_snapshot(root: str | Path) -> Optional[Dict[str, Any]]:
    """Read the latest snapshot, or ``None`` when absent/unreadable.

    Corrupt files are treated as absent (never trusted, never fatal): low-RAM
    machines may interrupt a write, and readers must degrade honestly.
    """
    path = Path(root) / Path(SNAPSHOT_RELATIVE_PATH)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def snapshot_age_seconds(snapshot: Dict[str, Any],
                         now: Optional[float] = None) -> float:
    """Wall-clock age of a snapshot; ``inf`` when it has no timestamp."""
    try:
        updated = float(snapshot.get("updated_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return float("inf")
    if updated <= 0:
        return float("inf")
    return max(0.0, (now if now is not None else time.time()) - updated)


def snapshot_is_fresh(snapshot: Dict[str, Any]) -> bool:
    age = snapshot_age_seconds(snapshot)
    return age <= FRESHNESS_SECONDS


def snapshot_summary(root: str | Path) -> Dict[str, Any]:
    """Human/UI-ready summary of the persisted state.

    Returns ``{"available": False, ...}`` when there is no snapshot yet —
    the UI shows "engine has not run here" rather than stale idle state.
    """
    snapshot = read_snapshot(root)
    if snapshot is None:
        return {"available": False,
                "reason": "no Native AI state snapshot under "
                          ".forge/native/state.json — the engine has not "
                          "run in this project yet"}
    age = snapshot_age_seconds(snapshot)
    return {
        "available": True,
        "fresh": age <= FRESHNESS_SECONDS,
        "age_seconds": round(age, 1),
        "snapshot": snapshot,
    }
