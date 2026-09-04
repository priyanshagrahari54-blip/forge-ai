from __future__ import annotations

from dataclasses import dataclass

from forge.core.task_engine import TaskEngine


@dataclass(frozen=True)
class DependencyCycle:
    tasks: tuple[str, ...]

    def __str__(self) -> str:
        return " -> ".join(self.tasks)


class TaskDependencyGraph:
    """Dependency graph for task execution ordering and cycle detection."""

    def __init__(self, engine: TaskEngine) -> None:
        self.engine = engine

    def dependencies(self, task_id: str) -> list[str]:
        """Return tasks that the given task depends on."""
        return list(self.engine._find(task_id).dependencies)

    def dependents(self, task_id: str) -> list[str]:
        """Return tasks that directly depend on the given task."""
        self.engine._find(task_id)

        return [
            task.id
            for task in self.engine.tasks
            if task_id in task.dependencies
        ]

    def find_cycle(self) -> DependencyCycle | None:
        """Return one dependency cycle, if one exists."""
        visiting: set[str] = set()
        visited: set[str] = set()
        path: list[str] = []

        def visit(task_id: str) -> DependencyCycle | None:
            if task_id in visiting:
                start = path.index(task_id)
                return DependencyCycle(tuple(path[start:] + [task_id]))

            if task_id in visited:
                return None

            visiting.add(task_id)
            path.append(task_id)

            task = self.engine._find(task_id)

            for dependency in task.dependencies:
                cycle = visit(dependency)
                if cycle is not None:
                    return cycle

            path.pop()
            visiting.remove(task_id)
            visited.add(task_id)
            return None

        for task in self.engine.tasks:
            cycle = visit(task.id)
            if cycle is not None:
                return cycle

        return None

    def validate(self) -> None:
        """Validate that the dependency graph contains no cycles."""
        cycle = self.find_cycle()

        if cycle is not None:
            raise ValueError(
                f"Task dependency cycle detected: {cycle}"
            )

    def topological_order(self) -> list[str]:
        """Return a deterministic dependency-first execution order."""
        self.validate()

        indegree = {task.id: 0 for task in self.engine.tasks}
        dependents: dict[str, list[str]] = {
            task.id: [] for task in self.engine.tasks
        }

        for task in self.engine.tasks:
            for dependency in task.dependencies:
                indegree[task.id] += 1
                dependents[dependency].append(task.id)

        ready = sorted(
            task_id
            for task_id, degree in indegree.items()
            if degree == 0
        )

        result: list[str] = []

        while ready:
            task_id = ready.pop(0)
            result.append(task_id)

            for dependent in sorted(dependents[task_id]):
                indegree[dependent] -= 1

                if indegree[dependent] == 0:
                    ready.append(dependent)

            ready.sort()

        if len(result) != len(indegree):
            raise ValueError("Unable to determine task execution order.")

        return result
