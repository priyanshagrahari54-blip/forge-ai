from __future__ import annotations

import pytest

from forge.core.dependency_graph import (
    DependencyCycle,
    TaskDependencyGraph,
)
from forge.core.task_engine import TaskEngine


def build_engine() -> TaskEngine:
    engine = TaskEngine()

    engine.add("setup", "Set up project")
    engine.add("code", "Write code", dependencies=["setup"])
    engine.add("tests", "Write tests", dependencies=["code"])
    engine.add("docs", "Write documentation", dependencies=["code"])

    return engine


def test_dependencies() -> None:
    engine = build_engine()
    graph = TaskDependencyGraph(engine)

    assert graph.dependencies("code") == ["setup"]
    assert graph.dependencies("setup") == []


def test_dependents() -> None:
    engine = build_engine()
    graph = TaskDependencyGraph(engine)

    assert graph.dependents("setup") == ["code"]
    assert graph.dependents("code") == ["tests", "docs"]


def test_topological_order_puts_dependencies_first() -> None:
    engine = build_engine()
    graph = TaskDependencyGraph(engine)

    order = graph.topological_order()

    assert order.index("setup") < order.index("code")
    assert order.index("code") < order.index("tests")
    assert order.index("code") < order.index("docs")


def test_topological_order_is_deterministic() -> None:
    engine = build_engine()
    graph = TaskDependencyGraph(engine)

    assert graph.topological_order() == graph.topological_order()


def test_no_cycle() -> None:
    engine = build_engine()
    graph = TaskDependencyGraph(engine)

    assert graph.find_cycle() is None
    graph.validate()


def test_direct_cycle_is_detected() -> None:
    engine = TaskEngine()
    engine.add("a", "Task A")
    engine.add("b", "Task B")

    engine.add_dependency("a", "b")
    engine.add_dependency("b", "a")

    graph = TaskDependencyGraph(engine)

    cycle = graph.find_cycle()

    assert isinstance(cycle, DependencyCycle)
    assert cycle.tasks == ("a", "b", "a")


def test_indirect_cycle_is_detected() -> None:
    engine = TaskEngine()
    engine.add("a", "Task A")
    engine.add("b", "Task B")
    engine.add("c", "Task C")

    engine.add_dependency("a", "b")
    engine.add_dependency("b", "c")
    engine.add_dependency("c", "a")

    graph = TaskDependencyGraph(engine)

    cycle = graph.find_cycle()

    assert cycle is not None
    assert cycle.tasks == ("a", "b", "c", "a")


def test_validate_reports_clear_cycle_error() -> None:
    engine = TaskEngine()
    engine.add("a", "Task A")
    engine.add("b", "Task B")

    engine.add_dependency("a", "b")
    engine.add_dependency("b", "a")

    graph = TaskDependencyGraph(engine)

    with pytest.raises(
        ValueError,
        match="Task dependency cycle detected",
    ):
        graph.validate()


def test_topological_order_rejects_cycles() -> None:
    engine = TaskEngine()
    engine.add("a", "Task A")
    engine.add("b", "Task B")

    engine.add_dependency("a", "b")
    engine.add_dependency("b", "a")

    graph = TaskDependencyGraph(engine)

    with pytest.raises(ValueError):
        graph.topological_order()
