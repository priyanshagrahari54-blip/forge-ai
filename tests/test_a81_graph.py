"""A81 dependency-aware task graph.

Sequential / parallel / dependent / blocked tasks, cycle detection,
dangling and self dependencies, deterministic topological order,
conflict detection (write-set overlap, shared exclusive resources,
sequential lanes), and failure-propagation queries.
"""
from __future__ import annotations

import pytest

from forge.orchestration.graph import (SEQUENTIAL_RESOURCE, TaskGraph,
                                       TaskKind, TaskStatus)


def _diamond():
    g = TaskGraph("diamond")
    g.add_task("a", "base", "planner")
    g.add_task("b", "left", "coder", dependencies=["a"],
               writes=["b.py"])
    g.add_task("c", "right", "researcher", dependencies=["a"])
    g.add_task("d", "join", "reviewer", dependencies=["b", "c"],
               reads=["b.py"])
    return g


def test_diamond_graph_validates_and_orders():
    g = _diamond()
    g.validate()
    order = g.topological_order()
    assert order[0] == "a"
    assert order[-1] == "d"
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_topological_order_is_deterministic_with_priority():
    def build():
        g = TaskGraph()
        g.add_task("low", "x", "planner", priority=0, created_at=2.0)
        g.add_task("high", "y", "planner", priority=5, created_at=9.0)
        g.add_task("mid", "z", "planner", priority=1, created_at=1.0)
        return g
    assert build().topological_order() == ["high", "mid", "low"]
    assert build().topological_order() == ["high", "mid", "low"]


def test_cycle_detection():
    g = TaskGraph()
    g.add_task("a", "x", "planner")
    g.add_task("b", "y", "coder")
    g.add_task("c", "z", "tester")
    g.link("a", "b")
    g.link("b", "c")
    g.link("c", "a")  # closes the cycle
    cycle = g.find_cycle()
    assert cycle and len(cycle) >= 3
    assert cycle[0] == cycle[-1]
    with pytest.raises(ValueError, match="cycle"):
        g.validate()
    with pytest.raises(ValueError, match="cycle"):
        g.topological_order()


def test_link_requires_existing_tasks():
    g = TaskGraph()
    g.add_task("a", "x", "planner")
    with pytest.raises(KeyError):
        g.link("a", "ghost")
    with pytest.raises(ValueError, match="itself"):
        g.link("a", "a")


def test_self_dependency_rejected():
    g = TaskGraph()
    with pytest.raises(ValueError, match="itself"):
        g.add_task("a", "x", "planner", dependencies=["a"])


def test_dangling_dependency_rejected():
    g = TaskGraph()
    with pytest.raises(ValueError, match="unknown task"):
        g.add_task("a", "x", "planner", dependencies=["ghost"])


def test_duplicate_task_id_rejected():
    g = TaskGraph()
    g.add_task("a", "x", "planner")
    with pytest.raises(ValueError, match="already exists"):
        g.add_task("a", "y", "coder")


def test_unknown_role_rejected_at_construction():
    g = TaskGraph()
    with pytest.raises(KeyError):
        g.add_task("a", "x", "wizard")


def test_dependent_kind_requires_dependencies():
    g = TaskGraph()
    g.add_task("a", "x", "planner", kind=TaskKind.DEPENDENT)
    with pytest.raises(ValueError, match="DEPENDENT"):
        g.validate()
    g2 = TaskGraph()
    g2.add_task("a", "x", "planner")
    g2.add_task("b", "y", "coder", kind="dependent", dependencies=["a"])
    g2.validate()


def test_sequential_tasks_serialize_against_each_other():
    g = TaskGraph()
    g.add_task("s1", "x", "planner", kind="sequential")
    g.add_task("s2", "y", "coder", kind="sequential")
    g.add_task("p", "z", "tester", kind="parallel")
    reasons = g.conflicts("s1", "s2")
    assert any("sequential" in r for r in reasons)
    # a sequential task does not conflict with a parallel one
    assert g.conflicts("s1", "p") == ()
    # the sequential lane shows up as an exclusive lock key
    assert SEQUENTIAL_RESOURCE in g.get("s1").lock_keys[1]
    assert SEQUENTIAL_RESOURCE not in g.get("p").lock_keys[1]


def test_write_overlap_is_a_conflict():
    g = TaskGraph()
    g.add_task("w1", "x", "coder", writes=["a.py"])
    g.add_task("w2", "y", "debugger", writes=["a.py", "b.py"])
    reasons = g.conflicts("w1", "w2")
    assert any("a.py" in r for r in reasons)


def test_read_write_overlap_is_not_a_conflict():
    g = TaskGraph()
    g.add_task("w", "x", "coder", writes=["a.py"])
    g.add_task("r", "y", "reviewer", reads=["a.py"])
    # the lock layer serializes read/write via shared/exclusive keys;
    # the graph-level conflict is write/write only.
    assert g.conflicts("w", "r") == ()
    # but the lock keys are mutually exclusive:
    shared_r, excl_r = g.get("r").lock_keys
    assert "a.py" in shared_r and "a.py" not in excl_r
    shared_w, excl_w = g.get("w").lock_keys
    assert "a.py" in excl_w and "a.py" not in shared_w


def test_shared_resource_is_a_conflict():
    g = TaskGraph()
    g.add_task("t1", "x", "tester", resources={"terminal"})
    g.add_task("t2", "y", "security", resources={"terminal"})
    reasons = g.conflicts("t1", "t2")
    assert any("terminal" in r for r in reasons)


def test_blocked_task_is_a_status_and_is_reversible():
    g = _diamond()
    g.block("b", "operator hold")
    assert g.get("b").status == TaskStatus.BLOCKED
    assert g.get("b").blocked_reason == "operator hold"
    g.unblock("b")
    assert g.get("b").status == TaskStatus.PENDING
    assert g.get("b").blocked_reason == ""
    with pytest.raises(ValueError):
        g.unblock("b")  # no longer blocked
    g.block("b")
    with pytest.raises(ValueError, match="state"):
        g.block("b")  # double block


def test_dependents_and_transitive_dependents():
    g = _diamond()
    assert [t.id for t in g.dependents("a")] == ["b", "c"]
    assert g.transitive_dependents("a") == {"b", "c", "d"}
    assert g.transitive_dependents("b") == {"d"}
    assert g.transitive_dependents("d") == set()


def test_graph_serialization_roundtrip():
    g = _diamond()
    g.block("c", "hold")
    data = g.to_dict()
    clone = TaskGraph.from_dict(data)
    assert [t.id for t in clone.tasks] == [t.id for t in g.tasks]
    assert clone.get("c").status == TaskStatus.BLOCKED
    assert clone.get("c").blocked_reason == "hold"
    assert clone.topological_order() == g.topological_order()
    clone.validate()


def test_empty_graph_invalid():
    with pytest.raises(ValueError, match="empty"):
        TaskGraph().validate()
