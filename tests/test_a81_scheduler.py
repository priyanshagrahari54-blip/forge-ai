"""A81 parallel scheduler: the concurrency guarantees.

Race conditions, dependency ordering, conflicting edits, bounded
concurrency, priority, retries, cancellation, and failure isolation —
each asserted against observed scheduler behavior, not documentation.
"""
from __future__ import annotations

import threading
import time

import pytest

from forge.core.run_control import SupervisorControl
from forge.orchestration import (ParallelTaskScheduler, TaskGraph,
                                 TaskStatus)

PLANNER_RESULT = {"plan": ["s"], "requirements": "r", "summary": "ok"}
CODER_RESULT = {"files": ["a.py"], "summary": "coded"}
TESTER_RESULT = {"passed": True, "tests_run": 1, "failures": []}
REVIEWER_RESULT = {"verdict": "APPROVED", "findings": []}
RESEARCH_RESULT = {"summary": "s", "sources": [], "hotspots": []}
DEBUG_RESULT = {"root_cause": "c", "files": [], "summary": "s"}
RESULTS = {
    "planner": PLANNER_RESULT, "coder": CODER_RESULT,
    "tester": TESTER_RESULT, "reviewer": REVIEWER_RESULT,
    "researcher": RESEARCH_RESULT, "debugger": DEBUG_RESULT,
}


def _workers_for(*roles, overrides=None):
    workers = {}
    for role in roles:
        workers[role] = (lambda r=role: (lambda w: RESULTS[r]))()
    if overrides:
        workers.update(overrides)
    return workers


def _overlap_tracker():
    state = {"running": set(), "f_active": set(), "peak": 0, "f_peak": 0}
    lock = threading.Lock()

    def worker(w):
        is_f = "f.py" in w.task.writes
        with lock:
            state["running"].add(w.task.id)
            state["peak"] = max(state["peak"], len(state["running"]))
            if is_f:
                state["f_active"].add(w.task.id)
                state["f_peak"] = max(state["f_peak"],
                                      len(state["f_active"]))
        time.sleep(0.08)
        with lock:
            state["running"].remove(w.task.id)
            if is_f:
                state["f_active"].remove(w.task.id)
        return RESULTS[w.task.role]
    return state, worker


# ---------------------------------------------------------------- race ---

def test_race_many_readers_one_writer_no_lost_updates(tmp_path):
    """Classic lost-update race: 8 tasks append to one file.

    The writer holds the file exclusively, so appends serialize; every
    one of the 8 appends survives.
    """
    path = tmp_path / "counter.txt"
    path.write_text("")
    g = TaskGraph()
    for i in range(8):
        g.add_task(f"app{i}", f"append {i}", "coder",
                   reads=["counter.txt"], writes=["counter.txt"])

    def writer(w):
        time.sleep(0.005)  # widen the race window
        current = path.read_text()
        time.sleep(0.005)
        path.write_text(current + "x")
        return {"files": ["counter.txt"], "summary": "appended"}

    report = ParallelTaskScheduler(
        g, {"coder": writer}, max_workers=8).run()
    assert report.status == "SUCCEEDED"
    assert path.read_text() == "x" * 8  # no lost updates


def test_race_independent_writers_distinct_files_run_concurrently():
    g = TaskGraph()
    for i in range(4):
        g.add_task(f"w{i}", f"write f{i}", "coder",
                   writes=[f"f{i}.py"])
    state, worker = _overlap_tracker()
    report = ParallelTaskScheduler(g, {"coder": worker},
                                   max_workers=4).run()
    assert report.status == "SUCCEEDED"
    assert state["peak"] == 4  # all four really ran in parallel


def test_conflicting_edits_never_overlap():
    """Two tasks writing the same file: never concurrent, both finish."""
    g = TaskGraph()
    g.add_task("w1", "write f", "coder", writes=["f.py"])
    g.add_task("w2", "write f again", "debugger", writes=["f.py"])
    state, worker = _overlap_tracker()
    events = []
    report = ParallelTaskScheduler(
        g, {"coder": worker, "debugger": worker}, max_workers=4,
        on_event=lambda n, d: events.append((n, d))).run()
    assert report.status == "SUCCEEDED"
    assert state["f_peak"] == 1  # the conflict was serialized
    assert any(n == "conflict_serialized" for n, _ in events)


def test_sequential_lane_never_overlaps():
    g = TaskGraph()
    for i in range(3):
        g.add_task(f"s{i}", f"step {i}", "planner", kind="sequential")
    state, worker = _overlap_tracker()
    report = ParallelTaskScheduler(g, {"planner": worker},
                                   max_workers=3).run()
    assert report.status == "SUCCEEDED"
    assert state["peak"] == 1  # sequential tasks never ran together


# ---------------------------------------------------------- dependencies ---

def test_dependent_tasks_run_only_after_success():
    order = []
    lock = threading.Lock()

    def tracker(name):
        def worker(w):
            with lock:
                order.append(("start", name))
            time.sleep(0.05)
            with lock:
                order.append(("end", name))
            return RESULTS[w.task.role]
        return worker

    g = TaskGraph()
    g.add_task("a", "base", "planner")
    g.add_task("b", "child", "coder", dependencies=["a"])
    ParallelTaskScheduler(g, {"planner": tracker("a"),
                              "coder": tracker("b")}).run()
    assert order.index(("end", "a")) < order.index(("start", "b"))


def test_dependent_task_receives_structured_handoff_message():
    g = TaskGraph()
    g.add_task("a", "base", "planner")
    g.add_task("b", "child", "coder", dependencies=["a"])
    seen_context = {}

    def child(w):
        seen_context["context"] = w.context
        return CODER_RESULT
    report = ParallelTaskScheduler(
        g, {"planner": lambda w: PLANNER_RESULT, "coder": child}).run()
    assert report.status == "SUCCEEDED"
    handoffs = [m for m in seen_context["context"]
                if m["message_type"] == "task_result"]
    assert len(handoffs) == 1
    assert handoffs[0]["sender"] == "planner"
    assert handoffs[0]["receiver"] == "coder"
    # the supervisor request is also structured context
    assert any(m["message_type"] == "task_request"
               for m in seen_context["context"])


def test_all_messages_are_structured_records():
    g = TaskGraph()
    g.add_task("a", "base", "planner")
    g.add_task("b", "child", "tester", dependencies=["a"])
    report = ParallelTaskScheduler(
        g, {"planner": lambda w: PLANNER_RESULT,
            "tester": lambda w: TESTER_RESULT}).run()
    assert report.messages
    for message in report.messages:
        for key in ("sender", "receiver", "task_id", "message_type"):
            assert message[key]
        assert 0.0 <= message["confidence"] <= 1.0


# ---------------------------------------------------------- bounded ---

def test_bounded_concurrency_respects_max_workers():
    g = TaskGraph()
    for i in range(8):
        g.add_task(f"t{i}", f"work {i}", "planner")
    state, worker = _overlap_tracker()
    report = ParallelTaskScheduler(g, {"planner": worker},
                                   max_workers=3).run()
    assert report.status == "SUCCEEDED"
    assert state["peak"] <= 3


# ------------------------------------------------------------ priority ---

def test_priority_queue_order_when_slots_are_scarce():
    started_order = []
    lock = threading.Lock()

    def slow(w):
        with lock:
            started_order.append(w.task.id)
        time.sleep(0.1)
        return PLANNER_RESULT

    g = TaskGraph()
    g.add_task("low", "low", "planner", priority=0)
    g.add_task("high", "high", "planner", priority=10)
    g.add_task("mid", "mid", "planner", priority=5)
    report = ParallelTaskScheduler(g, {"planner": slow},
                                   max_workers=1).run()
    assert report.status == "SUCCEEDED"
    assert started_order == ["high", "mid", "low"]


# -------------------------------------------------------------- retries ---

def test_retries_until_success_with_backoff():
    attempts = {"n": 0}

    def flaky(w):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")
        return CODER_RESULT

    g = TaskGraph()
    g.add_task("t1", "flaky", "coder", max_retries=3)
    events = []
    report = ParallelTaskScheduler(
        g, {"coder": flaky}, retry_backoff=0.01,
        on_event=lambda n, d: events.append(n)).run()
    assert report.status == "SUCCEEDED"
    task = report.tasks[0]
    assert task.attempts == 3
    assert attempts["n"] == 3
    assert events.count("task_retried") == 2


def test_retries_exhausted_fails_with_last_error():
    g = TaskGraph()
    g.add_task("t1", "doomed", "coder", max_retries=2)

    def broken(w):
        raise RuntimeError("permanent")
    report = ParallelTaskScheduler(g, {"coder": broken}).run()
    assert report.status == "FAILED"
    task = report.tasks[0]
    assert task.status == TaskStatus.FAILED
    assert task.attempts == 3  # 1 + 2 retries
    assert "permanent" in task.error


def test_denied_tasks_are_never_retried():
    from forge.orchestration import TaskDeniedError

    attempts = {"n": 0}

    def denied(w):
        attempts["n"] += 1
        raise TaskDeniedError("dispatch denied by policy")

    g = TaskGraph()
    g.add_task("t1", "denied", "coder", max_retries=5)
    report = ParallelTaskScheduler(g, {"coder": denied}).run()
    assert report.status == "FAILED"
    task = report.tasks[0]
    assert task.status == TaskStatus.DENIED
    assert attempts["n"] == 1  # no retries after a denial


def test_result_schema_violation_is_a_retryable_failure():
    calls = {"n": 0}

    def bad_then_good(w):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"files": "not-a-list", "summary": "x"}  # violation
        return CODER_RESULT

    g = TaskGraph()
    g.add_task("t1", "schema", "coder", max_retries=1)
    report = ParallelTaskScheduler(g, {"coder": bad_then_good}).run()
    assert report.status == "SUCCEEDED"
    assert calls["n"] == 2
    assert report.tasks[0].attempts == 2


def test_result_over_role_limit_is_rejected():
    def huge(w):
        return {"files": [], "summary": "x" * (64 * 1024)}

    g = TaskGraph()
    g.add_task("t1", "huge", "coder", max_retries=0)
    report = ParallelTaskScheduler(g, {"coder": huge}).run()
    assert report.status == "FAILED"
    assert "exceeds role limit" in report.tasks[0].error


def test_timeout_budget_bounds_attempts():
    g = TaskGraph()
    g.add_task("t1", "slow", "planner", timeout=0.1, max_retries=0)

    def slow(w):
        time.sleep(0.5)
        return PLANNER_RESULT
    events = []
    started = time.monotonic()
    report = ParallelTaskScheduler(
        g, {"planner": slow},
        on_event=lambda n, d: events.append(n)).run()
    elapsed = time.monotonic() - started
    assert report.status == "FAILED"
    assert any(n == "task_timeout" for n in events)
    assert "budget" in report.tasks[0].error
    assert elapsed < 1.5


# -------------------------------------------------------- cancellation ---

def test_cancellation_before_start_cancels_everything():
    control = SupervisorControl()
    g = TaskGraph()
    g.add_task("a", "x", "planner")
    g.add_task("b", "y", "coder", dependencies=["a"])
    control.request_cancel()
    report = ParallelTaskScheduler(g, _workers_for("planner", "coder"),
                                   control=control).run()
    assert report.status == "CANCELLED"
    assert all(t.status == TaskStatus.CANCELLED for t in report.tasks)


def test_cancellation_during_execution_stops_admitting_work():
    control = SupervisorControl()
    g = TaskGraph()
    g.add_task("a", "long", "planner")
    g.add_task("b", "stuck", "coder")  # independent of a
    g.add_task("c", "stuck2", "tester")

    def planner_worker(w):
        time.sleep(0.4)  # no checkpoint, but finishes within the grace
        return PLANNER_RESULT

    def stuck(w):
        time.sleep(3.0)  # non-cooperative: never observes the cancel
        return CODER_RESULT if w.task.role == "coder" else TESTER_RESULT

    def cancel_soon():
        time.sleep(0.1)
        control.request_cancel()
    threading.Thread(target=cancel_soon).start()
    report = ParallelTaskScheduler(
        g, {"planner": planner_worker, "coder": stuck, "tester": stuck},
        control=control, cancel_wait=1.0).run()
    assert report.status == "CANCELLED"
    statuses = {t.id: t.status for t in report.tasks}
    # the cooperative task finished inside the grace period...
    assert statuses["a"] == TaskStatus.SUCCEEDED
    # ...the non-cooperative ones were cancelled after the grace.
    assert statuses["b"] == TaskStatus.CANCELLED
    assert statuses["c"] == TaskStatus.CANCELLED
    b = next(t for t in report.tasks if t.id == "b")
    assert "did not stop in time" in b.error


def test_cancellation_observed_at_checkpoints():
    from forge.core.run_control import TaskCancelled

    control = SupervisorControl()
    seen = {"cancelled": False}

    def cooperative(w):
        try:
            time.sleep(0.1)
            w.checkpoint()
            return PLANNER_RESULT
        except TaskCancelled:
            seen["cancelled"] = True
            raise

    def cancel_soon():
        time.sleep(0.05)
        control.request_cancel()
    threading.Thread(target=cancel_soon).start()
    g = TaskGraph()
    g.add_task("a", "coop", "planner")
    report = ParallelTaskScheduler(
        g, {"planner": cooperative}, control=control).run()
    assert report.status == "CANCELLED"
    assert seen["cancelled"] is True
    assert report.tasks[0].status == TaskStatus.CANCELLED


def test_pause_blocks_checkpoint_and_resume_continues():
    control = SupervisorControl()
    states = []

    def pausable(w):
        control.checkpoint()
        time.sleep(0.2)  # pause window
        control.checkpoint()
        return PLANNER_RESULT

    g = TaskGraph()
    g.add_task("a", "pause", "planner")
    scheduler = ParallelTaskScheduler(
        g, {"planner": pausable}, control=control)

    def driver():
        time.sleep(0.05)
        control.request_pause()
        time.sleep(0.3)
        states.append(control.paused)  # should actually be paused
        control.request_resume()

    threading.Thread(target=driver).start()
    started = time.monotonic()
    report = scheduler.run()
    elapsed = time.monotonic() - started
    assert report.status == "SUCCEEDED"
    assert states and states[0] is True
    assert elapsed >= 0.25  # the pause was honored, not skipped


# -------------------------------------------------- failure isolation ---

def test_failing_subtree_does_not_poison_independent_branches():
    g = TaskGraph()
    g.add_task("ok1", "fine", "planner")
    g.add_task("bad", "broken", "coder", max_retries=0)
    g.add_task("child", "child", "tester", dependencies=["bad"])
    g.add_task("grand", "grand", "reviewer", dependencies=["child"])
    g.add_task("indep", "independent", "researcher")
    report = ParallelTaskScheduler(
        g, {"planner": lambda w: PLANNER_RESULT,
            "coder": (lambda w: (_ for _ in ()).throw(
                RuntimeError("boom"))),
            "tester": lambda w: TESTER_RESULT,
            "reviewer": lambda w: REVIEWER_RESULT,
            "researcher": lambda w: RESEARCH_RESULT}).run()
    statuses = {t.id: t.status for t in report.tasks}
    assert statuses["ok1"] == TaskStatus.SUCCEEDED
    assert statuses["indep"] == TaskStatus.SUCCEEDED  # kept running
    assert statuses["bad"] == TaskStatus.FAILED
    assert statuses["child"] == TaskStatus.BLOCKED
    assert statuses["grand"] == TaskStatus.BLOCKED
    child = next(t for t in report.tasks if t.id == "child")
    assert "bad" in child.blocked_reason
    assert report.status == "FAILED"


def test_graph_block_and_unblock_during_run():
    # max_workers=1: a takes the single slot, so b stays PENDING and
    # can be blocked by the operator before it is ever admitted.
    g = TaskGraph()
    g.add_task("a", "long", "planner")
    g.add_task("b", "held", "coder")
    events = []

    def slow(w):
        time.sleep(0.3)
        return PLANNER_RESULT

    def run_and_unblock(scheduler):
        time.sleep(0.05)
        scheduler.block("b", "operator hold")
        time.sleep(0.1)
        scheduler.unblock("b")
    scheduler = ParallelTaskScheduler(
        g, {"planner": slow, "coder": lambda w: CODER_RESULT},
        max_workers=1, on_event=lambda n, d: events.append(n))
    threading.Thread(target=run_and_unblock, args=(scheduler,)).start()
    report = scheduler.run()
    assert report.status == "SUCCEEDED"
    assert "task_blocked" in events and "task_unblocked" in events
    assert all(t.status == TaskStatus.SUCCEEDED for t in report.tasks)


def test_blocked_task_waits_for_unblock_and_never_runs_blocked():
    # max_workers=1 keeps b PENDING (behind the slow planner) until the
    # operator unblocks it; a blocked task must never be admitted.
    started = {}
    g = TaskGraph()
    g.add_task("a", "long", "planner")
    g.add_task("b", "held", "coder")
    g.block("b", "on hold")
    ran = {"b": False, "b_start": None}

    def coder(w):
        ran["b"] = True
        ran["b_start"] = time.monotonic()
        return CODER_RESULT

    def late_unblock(scheduler, marker):
        time.sleep(0.2)
        marker["unblocked_at"] = time.monotonic()
        scheduler.unblock("b")
    marker = {}
    scheduler = ParallelTaskScheduler(
        g, {"planner": (lambda w: time.sleep(0.5) or PLANNER_RESULT),
            "coder": coder}, max_workers=1)
    threading.Thread(target=late_unblock, args=(scheduler, marker)).start()
    report = scheduler.run()
    assert report.status == "SUCCEEDED"
    assert ran["b"] is True  # it ran, but only after unblock
    assert ran["b_start"] >= marker["unblocked_at"] - 0.01


# ------------------------------------------------------------ deadlock ---

def test_no_deadlock_with_mutually_exclusive_resources():
    """Two tasks each needing both shared resources, in different
    orders: atomic admission makes this deadlock-free."""
    g = TaskGraph()
    g.add_task("t1", "x", "coder", resources={"db", "cache"})
    g.add_task("t2", "y", "debugger", resources={"cache", "db"})
    state, worker = _overlap_tracker()

    def slow(w):
        time.sleep(0.05)
        return RESULTS[w.task.role]
    started = time.monotonic()
    report = ParallelTaskScheduler(
        g, {"coder": slow, "debugger": slow}, max_workers=2).run()
    elapsed = time.monotonic() - started
    assert report.status == "SUCCEEDED"
    assert elapsed < 10  # no hang


def test_scheduler_snapshot_exposes_lock_state():
    g = TaskGraph()
    g.add_task("a", "long", "planner")
    g.add_task("b", "conflict", "coder", writes=["a.py"])
    seen = {}

    def slow(w):
        if w.task.id == "a":
            time.sleep(0.2)
        return RESULTS[w.task.role]

    def capture(scheduler):
        time.sleep(0.05)
        seen["snap"] = scheduler.snapshot()
    scheduler = ParallelTaskScheduler(
        g, {"planner": (lambda w: (time.sleep(0.2), PLANNER_RESULT)[1]),
            "coder": (lambda w: (time.sleep(0.2), CODER_RESULT)[1])})
    threading.Thread(target=capture, args=(scheduler,)).start()
    report = scheduler.run()
    assert report.status == "SUCCEEDED"
    assert "activity" in seen["snap"]
    assert "locks" in seen["snap"]
    assert "tasks" in seen["snap"]


# ---------------------------------------------------------- misc ---

def test_missing_worker_role_rejected():
    g = TaskGraph()
    g.add_task("a", "x", "planner")
    with pytest.raises(ValueError, match="worker"):
        ParallelTaskScheduler(g, {})


def test_max_workers_must_be_positive():
    g = TaskGraph()
    g.add_task("a", "x", "planner")
    with pytest.raises(ValueError):
        ParallelTaskScheduler(g, _workers_for("planner"), max_workers=0)


def test_run_report_carries_events_and_counts():
    g = TaskGraph()
    g.add_task("a", "x", "planner")
    g.add_task("b", "y", "tester", dependencies=["a"])
    report = ParallelTaskScheduler(
        g, {"planner": lambda w: PLANNER_RESULT,
            "tester": lambda w: TESTER_RESULT}).run()
    data = report.to_dict()
    assert data["status"] == "SUCCEEDED"
    assert data["counts"]["SUCCEEDED"] == 2
    assert data["succeeded"] is True
    names = {e["name"] for e in data["events"]}
    assert {"run_started", "run_finished", "task_started",
            "task_finished", "lock_acquired", "lock_released"} <= names
    assert data["duration_seconds"] >= 0
