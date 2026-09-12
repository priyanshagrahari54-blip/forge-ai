"""A81 persistent execution state.

The state store journals every transition as it happens; after a
crash the graph reloads with terminal states preserved and RUNNING
tasks re-admitted as PENDING, so a restarted scheduler continues
instead of starting over.
"""
from __future__ import annotations

import time

from forge.orchestration import (ExecutionStateStore, ParallelTaskScheduler,
                                 TaskGraph, TaskStatus)

PLANNER_RESULT = {"plan": ["s"], "requirements": "r", "summary": "ok"}
CODER_RESULT = {"files": ["a.py"], "summary": "coded"}
TESTER_RESULT = {"passed": True, "tests_run": 1, "failures": []}


def test_run_and_transitions_are_durable(tmp_path):
    store = ExecutionStateStore(tmp_path / "exec.db")
    g = TaskGraph()
    g.add_task("a", "base", "planner")
    g.add_task("b", "child", "coder", dependencies=["a"])

    def slow_a(w):
        time.sleep(0.2)
        return PLANNER_RESULT
    scheduler = ParallelTaskScheduler(
        g, {"planner": slow_a, "coder": lambda w: CODER_RESULT},
        state_store=store, run_id="run-1", requirement="demo",
        project="demo")
    report = scheduler.run()
    assert report.status == "SUCCEEDED"

    loaded = store.load_run("run-1")
    assert loaded["run"]["status"] == "SUCCEEDED"
    assert loaded["run"]["project"] == "demo"
    by_id = {t["id"]: t for t in loaded["tasks"]}
    assert by_id["a"]["status"] == "SUCCEEDED"
    assert by_id["b"]["status"] == "SUCCEEDED"
    assert by_id["b"]["result"] == CODER_RESULT
    # structured messages journaled (request + handoff + result)
    types = {m["message_type"] for m in loaded["messages"]}
    assert {"task_request", "task_result"} <= types
    # events journaled
    names = {e["name"] for e in loaded["events"]}
    assert {"run_started", "run_finished", "task_started",
            "task_finished"} <= names
    # attempts journaled
    assert loaded["attempts"]
    assert all(a["outcome"] == "SUCCEEDED" for a in loaded["attempts"])


def test_failed_attempts_are_recorded_with_errors(tmp_path):
    store = ExecutionStateStore(tmp_path / "exec.db")
    g = TaskGraph()
    g.add_task("a", "flaky", "coder", max_retries=2)
    calls = {"n": 0}

    def flaky(w):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"fail {calls['n']}")
        return CODER_RESULT
    scheduler = ParallelTaskScheduler(
        g, {"coder": flaky}, state_store=store, run_id="run-2")
    report = scheduler.run()
    assert report.status == "SUCCEEDED"
    attempts = store.load_run("run-2")["attempts"]
    assert [a["outcome"] for a in attempts] == ["FAILED", "FAILED",
                                                 "SUCCEEDED"]
    assert "fail 1" in attempts[0]["error"]


def test_resume_after_crash_continues_not_restarts(tmp_path):
    """First run: a fails (crash), c (independent) succeeds.

    Resuming from the store must re-run a and its dependent b, and
    must NOT re-run the already-succeeded c.
    """
    store = ExecutionStateStore(tmp_path / "exec.db")
    ran = {"a": 0, "b": 0, "c": 0}
    first_attempt = {"a": False}

    def planner(w):
        ran["a"] += 1
        if not first_attempt["a"]:
            first_attempt["a"] = True
            raise RuntimeError("crash")
        return PLANNER_RESULT

    def coder(w):
        ran["b"] += 1
        return CODER_RESULT

    def tester(w):
        ran["c"] += 1
        time.sleep(0.1)
        return TESTER_RESULT

    def build():
        g = TaskGraph()
        g.add_task("a", "base", "planner")
        g.add_task("b", "child", "coder", dependencies=["a"])
        g.add_task("c", "indep", "tester")
        return g

    sched1 = ParallelTaskScheduler(
        build(), {"planner": planner, "coder": coder, "tester": tester},
        state_store=store, run_id="run-3")
    report1 = sched1.run()
    assert report1.status == "FAILED"
    assert ran == {"a": 1, "b": 0, "c": 1}

    # Resume: rebuild the graph from the store.
    resumed = store.load_graph("run-3")
    assert resumed.get("a").status == TaskStatus.PENDING
    assert resumed.get("c").status == TaskStatus.SUCCEEDED
    sched2 = ParallelTaskScheduler(
        resumed, {"planner": planner, "coder": coder, "tester": tester},
        state_store=store, run_id="run-3")
    report2 = sched2.run()
    assert report2.status == "SUCCEEDED"
    assert ran == {"a": 2, "b": 1, "c": 1}


def test_running_task_readmitted_as_pending_on_reload(tmp_path):
    store = ExecutionStateStore(tmp_path / "exec.db")
    g = TaskGraph()
    g.add_task("a", "x", "planner")
    sched = ParallelTaskScheduler(
        g, {"planner": lambda w: PLANNER_RESULT},
        state_store=store, run_id="run-4")
    # Manually record a RUNNING task (crashed mid-attempt).
    sched.run()  # ensure run exists
    from forge.orchestration import TaskNode
    task = TaskNode(id="a", description="x", role="planner")
    task.status = TaskStatus.RUNNING
    store.save_task("run-4", task)
    resumed = store.load_graph("run-4")
    assert resumed.get("a").status == TaskStatus.PENDING
    assert resumed.get("a").started_at is None


def test_list_runs_and_unknown_run():
    import pytest

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store = ExecutionStateStore(f"{tmp}/e.db")
        store.create_run("r1", "proj", "req", {"max_workers": 2})
        store.create_run("r2", "proj", "req2", {})
        runs = store.list_runs("proj")
        assert {r["run_id"] for r in runs} == {"r1", "r2"}
        assert store.list_runs("other") == []
        with pytest.raises(KeyError):
            store.load_run("missing")
