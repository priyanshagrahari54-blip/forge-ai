"""Dependency-aware automatic milestone execution for Forge.

The orchestrator turns a milestone plan into a resumable execution loop. It
never requires an interactive "continue?" decision between ordinary
milestones: completed milestones unlock their dependants, failures are retried
within a bounded budget, and state/checkpoints are persisted after every
transition.

This module deliberately does not invent implementation work or bypass
approval/security gates. A milestone executor is supplied by the surrounding
Forge worker and is responsible for the actual code/build/test operation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
from pathlib import Path
from time import time
from typing import Any, Callable, Iterable


class MilestoneStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class MilestoneSpec:
    """A dependency-aware unit of autonomous engineering work."""

    id: str
    title: str
    description: str = ""
    depends_on: tuple[str, ...] = ()
    max_attempts: int = 3
    auto_continue: bool = True
    requires_approval: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MilestoneState:
    id: str
    status: str = MilestoneStatus.PENDING.value
    attempts: int = 0
    last_error: str = ""
    checkpoint: str = ""
    updated_at: float = field(default_factory=time)
    result: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MilestoneEvent:
    milestone_id: str
    event: str
    status: str
    attempt: int
    detail: str = ""
    timestamp: float = 0.0


@dataclass
class AutoRunResult:
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    events: list[MilestoneEvent] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.failed and not self.blocked


Executor = Callable[[MilestoneSpec, MilestoneState], Any]
Checkpoint = Callable[[MilestoneSpec, MilestoneState, str], None]
ApprovalCheck = Callable[[MilestoneSpec], bool]


class AutoMilestoneRunner:
    """Run all dependency-ready milestones until the plan reaches a fixed point.

    State is persisted after every meaningful transition. A process restart can
    therefore resume from the last completed/failed milestone without asking
    the user to manually advance the plan.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        milestones: Iterable[MilestoneSpec],
        *,
        state_path: str | Path = ".forge/auto-milestones.json",
        checkpoint: Checkpoint | None = None,
        approval_check: ApprovalCheck | None = None,
    ) -> None:
        specs = list(milestones)
        self.specs = {item.id: item for item in specs}
        if len(self.specs) != len(specs):
            raise ValueError("Milestone IDs must be unique")
        self._validate_graph()
        self.state_path = Path(state_path)
        self.checkpoint = checkpoint
        self.approval_check = approval_check or (lambda _spec: True)
        self.states: dict[str, MilestoneState] = {
            item.id: MilestoneState(id=item.id) for item in specs
        }
        self.events: list[MilestoneEvent] = []
        self.load()

    def _validate_graph(self) -> None:
        for spec in self.specs.values():
            unknown = [dep for dep in spec.depends_on if dep not in self.specs]
            if unknown:
                raise ValueError(f"{spec.id} depends on unknown milestones: {unknown}")
            if spec.max_attempts < 1:
                raise ValueError(f"{spec.id} max_attempts must be >= 1")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("Milestone dependency graph contains a cycle")
            if node in visited:
                return
            visiting.add(node)
            for dep in self.specs[node].depends_on:
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        for node in self.specs:
            visit(node)

    def load(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError("Unsupported auto-milestone state schema")
        for item in data.get("states", []):
            milestone_id = item.get("id")
            if milestone_id in self.specs:
                self.states[milestone_id] = MilestoneState(**item)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": time(),
            "states": [asdict(state) for state in self.states.values()],
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    def ready(self) -> list[MilestoneSpec]:
        """Return all milestones whose dependencies are complete."""
        result: list[MilestoneSpec] = []
        for spec in self.specs.values():
            state = self.states[spec.id]
            if state.status != MilestoneStatus.PENDING.value:
                continue
            if any(self.states[dep].status != MilestoneStatus.PASSED.value for dep in spec.depends_on):
                continue
            result.append(spec)
        return result

    def _emit(self, milestone_id: str, event: str, status: str, attempt: int, detail: str = "") -> None:
        self.events.append(MilestoneEvent(
            milestone_id=milestone_id,
            event=event,
            status=status,
            attempt=attempt,
            detail=detail,
            timestamp=time(),
        ))

    def _set_status(self, spec: MilestoneSpec, status: MilestoneStatus, *, detail: str = "") -> None:
        state = self.states[spec.id]
        state.status = status.value
        state.last_error = detail if status in {MilestoneStatus.FAILED, MilestoneStatus.BLOCKED} else state.last_error
        state.updated_at = time()
        self._emit(spec.id, status.value, status.value, state.attempts, detail)
        self.save()

    def run(self, executor: Executor) -> AutoRunResult:
        """Execute ready milestones and automatically advance to the next ones."""
        result = AutoRunResult()

        # A failed milestone is terminal for this run unless the caller resets
        # it. This prevents an infinite retry loop across process restarts.
        for spec in self.specs.values():
            state = self.states[spec.id]
            if state.status == MilestoneStatus.FAILED.value and state.attempts >= spec.max_attempts:
                result.failed.append(spec.id)

        while True:
            ready = self.ready()
            if not ready:
                break

            progressed = False
            for spec in ready:
                state = self.states[spec.id]
                if spec.requires_approval and not self.approval_check(spec):
                    state.status = MilestoneStatus.BLOCKED.value
                    state.last_error = "approval required"
                    state.updated_at = time()
                    self._emit(spec.id, "blocked", state.status, state.attempts, state.last_error)
                    self.save()
                    result.blocked.append(spec.id)
                    progressed = True
                    continue

                if not spec.auto_continue:
                    state.status = MilestoneStatus.BLOCKED.value
                    state.last_error = "auto_continue disabled"
                    state.updated_at = time()
                    self._emit(spec.id, "blocked", state.status, state.attempts, state.last_error)
                    self.save()
                    result.blocked.append(spec.id)
                    progressed = True
                    continue

                while state.attempts < spec.max_attempts:
                    state.attempts += 1
                    state.status = MilestoneStatus.RUNNING.value
                    state.updated_at = time()
                    self._emit(spec.id, "started", state.status, state.attempts)
                    self.save()
                    try:
                        value = executor(spec, state)
                        state.result = value if isinstance(value, dict) else {"value": value}
                        state.status = MilestoneStatus.PASSED.value
                        state.last_error = ""
                        state.checkpoint = f"passed-attempt-{state.attempts}"
                        state.updated_at = time()
                        self._emit(spec.id, "passed", state.status, state.attempts)
                        self.save()
                        if self.checkpoint:
                            self.checkpoint(spec, state, "passed")
                        result.completed.append(spec.id)
                        progressed = True
                        break
                    except Exception as exc:  # executor decides whether retry is useful
                        state.last_error = str(exc)
                        state.status = MilestoneStatus.FAILED.value
                        state.checkpoint = f"failed-attempt-{state.attempts}"
                        state.updated_at = time()
                        self._emit(spec.id, "failed", state.status, state.attempts, state.last_error)
                        self.save()
                        if self.checkpoint:
                            self.checkpoint(spec, state, "failed")
                        if state.attempts >= spec.max_attempts:
                            result.failed.append(spec.id)
                            progressed = True
                            break

            if not progressed:
                break

        # Any pending milestone whose dependencies are terminally failed or
        # blocked is marked blocked rather than being silently left pending.
        for spec in self.specs.values():
            state = self.states[spec.id]
            if state.status != MilestoneStatus.PENDING.value:
                continue
            if any(self.states[dep].status in {MilestoneStatus.FAILED.value, MilestoneStatus.BLOCKED.value} for dep in spec.depends_on):
                self._set_status(spec, MilestoneStatus.BLOCKED, detail="dependency failed or blocked")
                result.blocked.append(spec.id)

        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "total": len(self.specs),
            "passed": sum(s.status == "passed" for s in self.states.values()),
            "running": sum(s.status == "running" for s in self.states.values()),
            "pending": sum(s.status == "pending" for s in self.states.values()),
            "failed": sum(s.status == "failed" for s in self.states.values()),
            "blocked": sum(s.status == "blocked" for s in self.states.values()),
            "ready": [spec.id for spec in self.ready()],
        }
