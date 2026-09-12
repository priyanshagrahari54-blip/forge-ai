"""Dependency-aware task graph (A81).

A :class:`TaskGraph` is the unit of concurrent execution. Tasks declare
their dependencies, the files they read and write, and the named shared
resources they need; the graph answers the supervisor's two scheduling
questions:

1. **Which tasks are ready?** — all dependencies succeeded, not
   explicitly blocked.
2. **Which of those may run concurrently?** — two tasks conflict when
   their write sets overlap, they share an exclusive resource, or both
   are ``sequential`` tasks (serialized against each other by an implicit
   exclusive resource). Conflicting tasks are never dispatched together;
   the scheduler serializes them instead.

Task kinds:

* ``PARALLEL`` — eligible to run alongside any non-conflicting task;
* ``SEQUENTIAL`` — mutually exclusive with every other sequential task
  in the graph (they form an ordered lane, never overlap);
* ``DEPENDENT`` — must declare at least one dependency; otherwise the
  graph is invalid.

``BLOCKED`` is a *status*, not a kind: a task is blocked explicitly by
the operator (until unblocked) or automatically when a dependency fails
(failure isolation). Blocked tasks never run and never run concurrently
with anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from forge.orchestration.roles import get_role_spec

#: Implicit exclusive resource shared by all SEQUENTIAL tasks.
SEQUENTIAL_RESOURCE = "__sequential__"


class TaskKind(str, Enum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    DEPENDENT = "dependent"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


#: States from which a task never runs (the scheduler stops considering
#: it). Blocked tasks are excluded only while blocked; unblocking a
#: pending-blocked task re-admits it.
TERMINAL_STATES = frozenset({
    TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.DENIED,
    TaskStatus.SKIPPED, TaskStatus.CANCELLED,
})

#: Failed-dependency propagation treats these as "did not succeed".
UNSUCCESSFUL = frozenset({
    TaskStatus.FAILED, TaskStatus.DENIED, TaskStatus.CANCELLED,
    TaskStatus.SKIPPED, TaskStatus.BLOCKED,
})


@dataclass
class TaskNode:
    """One task in the graph."""

    id: str
    description: str
    role: str
    kind: TaskKind = TaskKind.PARALLEL
    priority: int = 0
    created_at: float = 0.0
    dependencies: tuple[str, ...] = ()
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    resources: frozenset[str] = frozenset()
    max_retries: int = 0
    timeout: float | None = None
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: str = ""
    blocked_reason: str = ""
    started_at: float | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        self.kind = TaskKind(self.kind)
        self.status = TaskStatus(self.status)
        self.reads = frozenset(self.reads)
        self.writes = frozenset(self.writes)
        self.resources = frozenset(self.resources)
        get_role_spec(self.role)  # unknown role -> KeyError at construction
        if self.max_retries < 0:
            raise ValueError(f"Task {self.id}: max_retries must be >= 0")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"Task {self.id}: timeout must be positive")

    @property
    def lock_keys(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(shared, exclusive) resource keys this task needs.

        Read files are shared, written files exclusive; declared
        resources are always exclusive. Sequential tasks additionally
        take the implicit sequential-lane resource exclusively.
        """
        shared = tuple(sorted(self.reads))
        exclusive = tuple(sorted(self.writes | self.resources))
        if self.kind == TaskKind.SEQUENTIAL and \
                SEQUENTIAL_RESOURCE not in exclusive:
            exclusive = tuple(sorted(exclusive + (SEQUENTIAL_RESOURCE,)))
        return shared, exclusive

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "description": self.description,
            "role": self.role, "kind": self.kind.value,
            "priority": self.priority, "created_at": self.created_at,
            "dependencies": list(self.dependencies),
            "reads": sorted(self.reads), "writes": sorted(self.writes),
            "resources": sorted(self.resources),
            "max_retries": self.max_retries, "timeout": self.timeout,
            "status": self.status.value, "attempts": self.attempts,
            "result": self.result, "error": self.error,
            "blocked_reason": self.blocked_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskNode":
        return cls(
            id=data["id"], description=data["description"],
            role=data["role"], kind=TaskKind(data.get("kind", "parallel")),
            priority=data.get("priority", 0),
            created_at=data.get("created_at", 0.0),
            dependencies=tuple(data.get("dependencies", ())),
            reads=frozenset(data.get("reads", ())),
            writes=frozenset(data.get("writes", ())),
            resources=frozenset(data.get("resources", ())),
            max_retries=data.get("max_retries", 0),
            timeout=data.get("timeout"),
            status=TaskStatus(data.get("status", "PENDING")),
            attempts=data.get("attempts", 0),
            result=data.get("result"), error=data.get("error", ""),
            blocked_reason=data.get("blocked_reason", ""),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
        )


class TaskGraph:
    """A validated dependency graph of tasks."""

    def __init__(self, name: str = "graph") -> None:
        self.name = name
        self._tasks: dict[str, TaskNode] = {}
        self._order: list[str] = []

    # -- construction ---------------------------------------------------------

    def add_task(self, task_id: str, description: str, role: str, *,
                 kind: TaskKind | str = TaskKind.PARALLEL,
                 priority: int = 0, created_at: float = 0.0,
                 dependencies: tuple[str, ...] | list[str] | None = None,
                 reads: tuple[str, ...] | list[str] | set = (),
                 writes: tuple[str, ...] | list[str] | set = (),
                 resources: tuple[str, ...] | list[str] | set = (),
                 max_retries: int = 0,
                 timeout: float | None = None) -> TaskNode:
        if not task_id or not task_id.strip():
            raise ValueError("Task id must be a non-empty string")
        if task_id in self._tasks:
            raise ValueError(f"Task already exists: {task_id!r}")
        if not description or not description.strip():
            raise ValueError(f"Task {task_id!r} needs a description")
        for dependency in dependencies or ():
            if dependency == task_id:
                raise ValueError(
                    f"Task {task_id!r} cannot depend on itself")
            if dependency not in self._tasks:
                raise ValueError(
                    f"Task {task_id!r} depends on unknown task "
                    f"{dependency!r}")
        node = TaskNode(
            id=task_id, description=description.strip(), role=role,
            kind=TaskKind(kind), priority=priority,
            created_at=created_at, dependencies=tuple(dependencies or ()),
            reads=frozenset(reads), writes=frozenset(writes),
            resources=frozenset(resources), max_retries=max_retries,
            timeout=timeout)
        self._tasks[task_id] = node
        self._order.append(task_id)
        return node

    def link(self, task_id: str, dependency_id: str) -> TaskNode:
        """Add a dependency edge between two existing tasks.

        Both tasks must already exist; the edge is structural — a link
        that creates a cycle is recorded so
        :meth:`find_cycle` / :meth:`validate` can report it honestly.
        """
        task = self._find(task_id)
        self._find(dependency_id)
        if dependency_id == task_id:
            raise ValueError(f"Task {task_id!r} cannot depend on itself")
        if dependency_id not in task.dependencies:
            task.dependencies = task.dependencies + (dependency_id,)
        return task

    def block(self, task_id: str, reason: str = "") -> TaskNode:
        """Explicitly block a task (operator action). Reversible.

        Only PENDING tasks can be blocked: a running task must be
        cancelled, and terminal tasks are done.
        """
        task = self._find(task_id)
        if task.status != TaskStatus.PENDING:
            raise ValueError(
                f"Cannot block task {task_id!r} in state "
                f"{task.status.value}; only PENDING tasks can be "
                f"blocked")
        task.status = TaskStatus.BLOCKED
        task.blocked_reason = reason or "blocked by operator"
        return task

    def unblock(self, task_id: str) -> TaskNode:
        task = self._find(task_id)
        if task.status != TaskStatus.BLOCKED:
            raise ValueError(
                f"Task {task_id!r} is not blocked "
                f"(state {task.status.value})")
        task.status = TaskStatus.PENDING
        task.blocked_reason = ""
        return task

    # -- validation -------------------------------------------------------------

    def validate(self) -> None:
        """Full structural validation; raises ValueError on any problem."""
        if not self._tasks:
            raise ValueError("Task graph is empty")
        for task in self.tasks:
            for dependency in task.dependencies:
                if dependency == task.id:
                    raise ValueError(
                        f"Task {task.id!r} depends on itself")
                if dependency not in self._tasks:
                    raise ValueError(
                        f"Task {task.id!r} depends on unknown task "
                        f"{dependency!r}")
            if task.kind == TaskKind.DEPENDENT and not task.dependencies:
                raise ValueError(
                    f"Task {task.id!r} is DEPENDENT but declares no "
                    f"dependencies")
        cycle = self.find_cycle()
        if cycle:
            raise ValueError(
                "Task dependency cycle detected: " + " -> ".join(cycle))

    def find_cycle(self) -> list[str]:
        """Return one dependency cycle, or an empty list when acyclic."""
        state: dict[str, int] = {}
        path: list[str] = []

        def visit(task_id: str) -> list[str]:
            state[task_id] = 1  # visiting
            path.append(task_id)
            for dependency in self._tasks[task_id].dependencies:
                mark = state.get(dependency, 0)
                if mark == 1:
                    start = path.index(dependency)
                    return path[start:] + [dependency]
                if mark == 0:
                    cycle = visit(dependency)
                    if cycle:
                        return cycle
            path.pop()
            state[task_id] = 2  # done
            return []

        for task_id in self._order:
            if state.get(task_id, 0) == 0:
                cycle = visit(task_id)
                if cycle:
                    return cycle
        return []

    # -- queries ------------------------------------------------------------------

    @property
    def tasks(self) -> list[TaskNode]:
        return [self._tasks[task_id] for task_id in self._order]

    def get(self, task_id: str) -> TaskNode:
        return self._find(task_id)

    def by_role(self, role: str) -> list[TaskNode]:
        return [task for task in self.tasks if task.role == role]

    def dependents(self, task_id: str) -> list[TaskNode]:
        self._find(task_id)
        return [task for task in self.tasks
                if task_id in task.dependencies]

    def transitive_dependents(self, task_id: str) -> set[str]:
        """Every task that (transitively) depends on *task_id*."""
        self._find(task_id)
        seen: set[str] = set()
        stack = [task_id]
        while stack:
            current = stack.pop()
            for dependent in self.dependents(current):
                if dependent.id not in seen:
                    seen.add(dependent.id)
                    stack.append(dependent.id)
        return seen

    def topological_order(self) -> list[str]:
        """Deterministic dependency-first order.

        Ties break by (priority descending, created_at, id), so a
        rebuild of the same graph yields the same order.
        """
        self.validate()
        indegree = {task.id: len(task.dependencies) for task in self.tasks}
        dependents: dict[str, list[str]] = {task.id: []
                                            for task in self.tasks}
        for task in self.tasks:
            for dependency in task.dependencies:
                dependents[dependency].append(task.id)

        def sort_key(task_id: str) -> tuple:
            task = self._tasks[task_id]
            return (-task.priority, task.created_at, task.id)

        ready = sorted((task_id for task_id, degree in indegree.items()
                        if degree == 0), key=sort_key)
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for dependent in dependents[current]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
            ready.sort(key=sort_key)
        if len(order) != len(self._tasks):
            raise ValueError(
                "Task graph contains a cycle: "
                + " -> ".join(self.find_cycle()))
        return order

    def conflicts(self, a: TaskNode | str, b: TaskNode | str) -> tuple[str, ...]:
        """Reasons *a* and *b* must not run concurrently; () when safe."""
        if isinstance(a, str):
            a = self._find(a)
        if isinstance(b, str):
            b = self._find(b)
        if a.id == b.id:
            return ()
        reasons = []
        overlap = sorted(a.writes & b.writes)
        if overlap:
            reasons.append("write-set overlap: " + ", ".join(overlap))
        if a.kind == TaskKind.SEQUENTIAL and \
                b.kind == TaskKind.SEQUENTIAL:
            reasons.append("sequential tasks are serialized")
        shared_resources = sorted(a.resources & b.resources)
        if shared_resources:
            reasons.append("shared exclusive resource: "
                           + ", ".join(shared_resources))
        return tuple(reasons)

    def conflict_with_running(self, task: TaskNode,
                              running: list[TaskNode]
                              ) -> tuple[TaskNode | None, tuple[str, ...]]:
        """First running task *task* conflicts with, with reasons.

        Returns ``(None, ())`` when the task conflicts with nothing.
        """
        for other in running:
            reasons = self.conflicts(task, other)
            if reasons:
                return other, reasons
        return None, ()

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name,
                "tasks": [task.to_dict() for task in self.tasks]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskGraph":
        graph = cls(name=data.get("name", "graph"))
        tasks = data.get("tasks", [])
        for task_data in tasks:
            graph.add_task(
                task_data["id"], task_data["description"], task_data["role"],
                kind=task_data.get("kind", "parallel"),
                priority=task_data.get("priority", 0),
                created_at=task_data.get("created_at", 0.0),
                reads=task_data.get("reads", ()),
                writes=task_data.get("writes", ()),
                resources=task_data.get("resources", ()),
                max_retries=task_data.get("max_retries", 0),
                timeout=task_data.get("timeout"))
        for task_data in tasks:
            dependencies = task_data.get("dependencies", ())
            node = graph._tasks[task_data["id"]]
            node.dependencies = tuple(dependencies)
            node.status = TaskStatus(task_data.get("status", "PENDING"))
            node.attempts = task_data.get("attempts", 0)
            node.result = task_data.get("result")
            node.error = task_data.get("error", "")
            node.blocked_reason = task_data.get("blocked_reason", "")
            node.started_at = task_data.get("started_at")
            node.finished_at = task_data.get("finished_at")
        graph.validate()
        return graph

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for task in self.tasks:
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        return {
            "name": self.name,
            "total": len(self.tasks),
            "counts": counts,
            "roles": sorted({task.role for task in self.tasks}),
        }

    def _find(self, task_id: str) -> TaskNode:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise KeyError(f"Task not found: {task_id!r}") from None
