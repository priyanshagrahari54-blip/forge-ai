"""Canonical DAG scheduler tests (Session 10, Section 3).

Covers the scheduling contract itself: dependency-ordered execution,
bounded concurrency, deterministic terminal states, resource locks with
conflict detection, retries within budget, and the event log.
"""
from __future__ import annotations

import threading
import time

import pytest

from forge.core.dag_scheduler import (
    DAGScheduler,
    DAGSchedulerError,
    TaskResult,
)


# -- graph construction -----------------------------------------------------


def test_duplicate_and_unknown_dependencies_are_rejected():
    scheduler = DAGScheduler()
    scheduler.add_task("a")
    with pytest.raises(DAGSchedulerError):
        scheduler.add_task("a")
    with pytest.raises(DAGSchedulerError):
        scheduler.add_task("b", depends_on=["missing"])
    with pytest.raises(DAGSchedulerError):
        scheduler.add_task("c", depends_on=["c"])


def test_cycle_detection_rejects_cyclic_graphs():
    scheduler = DAGScheduler()
    scheduler.add_task("a")
    scheduler.add_task("b", depends_on=["a"])
    scheduler.add_task("c", depends_on=["b"])
    # Inject a cycle (the public API cannot create one, since tasks are
    # validated as they are added); the detector must catch it.
    scheduler._tasks["a"].depends_on = ("c",)
    with pytest.raises(DAGSchedulerError):
        scheduler._detect_cycle()


def test_invalid_budgets_are_rejected():
    scheduler = DAGScheduler()
    with pytest.raises(DAGSchedulerError):
        scheduler.add_task("a", max_attempts=0)
    with pytest.raises(DAGSchedulerError):
        scheduler.add_task("b", timeout=0)
    with pytest.raises(ValueError):
        DAGScheduler(max_workers=0)


# -- dependency order ---------------------------------------------------------


def test_dependency_order_is_respected():
    order: list[str] = []
    lock = threading.Lock()

    def executor(task, attempt, control, guard):
        with lock:
            order.append(task.task_id)
        return {"success": True, "output": "ok", "terminal": True}

    scheduler = DAGScheduler(max_workers=4)
    scheduler.add_task("a")
    scheduler.add_task("b", depends_on=["a"])
    scheduler.add_task("c", depends_on=["a"])
    scheduler.add_task("d", depends_on=["b", "c"])
    results = scheduler.run(executor)

    assert all(r.state == "SUCCEEDED" for r in results.values())
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_failed_dependency_skips_dependents_deterministically():
    def executor(task, attempt, control, guard):
        if task.task_id == "a":
            return {"success": False, "error": "boom", "terminal": True}
        return {"success": True, "output": "ok", "terminal": True}

    scheduler = DAGScheduler(max_workers=2)
    scheduler.add_task("a")
    scheduler.add_task("b", depends_on=["a"])
    scheduler.add_task("c", depends_on=["b"])
    results = scheduler.run(executor)

    assert results["a"].state == "FAILED"
    assert results["b"].state == "SKIPPED"
    assert "a" in results["b"].error
    assert results["c"].state == "SKIPPED"
    # Every task has a deterministic terminal state.
    assert len(results) == 3


# -- bounded concurrency ------------------------------------------------------


def test_concurrency_is_bounded():
    active = 0
    peak = 0
    lock = threading.Lock()

    def executor(task, attempt, control, guard):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"success": True, "output": "ok", "terminal": True}

    scheduler = DAGScheduler(max_workers=2)
    for name in ("t1", "t2", "t3", "t4", "t5"):
        scheduler.add_task(name)
    results = scheduler.run(executor)
    assert all(r.state == "SUCCEEDED" for r in results.values())
    assert peak <= 2, peak


# -- retries -------------------------------------------------------------------


def test_retries_within_budget_then_fail():
    calls = []

    def executor(task, attempt, control, guard):
        calls.append((task.task_id, attempt.generation))
        return {"success": False, "error": "always", "terminal": False}

    scheduler = DAGScheduler(max_workers=2)
    scheduler.add_task("flaky", max_attempts=3)
    results = scheduler.run(executor)

    assert results["flaky"].state == "FAILED"
    assert results["flaky"].attempts == 3
    # Generations are strictly increasing: each retry is a new attempt.
    generations = [generation for _, generation in calls]
    assert generations == sorted(generations)
    assert len(set(generations)) == 3
    assert "always" in results["flaky"].error


def test_terminal_failure_is_not_retried():
    calls = []

    def executor(task, attempt, control, guard):
        calls.append(1)
        return {"success": False, "error": "fatal", "terminal": True}

    scheduler = DAGScheduler(max_workers=1)
    scheduler.add_task("fatal", max_attempts=5)
    results = scheduler.run(executor)
    assert results["fatal"].state == "FAILED"
    assert calls == [1]  # no retry for a terminal failure


def test_success_on_retry_records_succeeded():
    calls = []

    def executor(task, attempt, control, guard):
        calls.append(1)
        if len(calls) == 1:
            return {"success": False, "error": "first", "terminal": False}
        return {"success": True, "output": "recovered", "terminal": True}

    scheduler = DAGScheduler(max_workers=1)
    scheduler.add_task("flaky", max_attempts=2)
    results = scheduler.run(executor)
    assert results["flaky"].state == "SUCCEEDED"
    assert results["flaky"].attempts == 2
    assert results["flaky"].output == "recovered"


# -- resource locks --------------------------------------------------------------


def test_resource_lock_conflict_is_deterministic():
    def executor(task, attempt, control, guard):
        time.sleep(0.2)
        return {"success": True, "output": "ok", "terminal": True}

    scheduler = DAGScheduler(max_workers=2)
    scheduler.add_task("holder", resources=["db"])
    scheduler.add_task("rival", resources=["db"])
    results = scheduler.run(executor)

    states = {results["holder"].state, results["rival"].state}
    # One gets the lock and succeeds; the other fails with LOCK_CONFLICT.
    # (Both are independent, so dispatch order decides the holder.)
    assert states == {"SUCCEEDED", "FAILED"}, states
    failed = next(r for r in results.values() if r.state == "FAILED")
    assert "LOCK_CONFLICT" in failed.error
    assert "db" in failed.error


def test_lock_is_released_after_finish():
    def executor(task, attempt, control, guard):
        return {"success": True, "output": "ok", "terminal": True}

    scheduler = DAGScheduler(max_workers=1)
    scheduler.add_task("first", resources=["db"])
    scheduler.add_task("second", depends_on=["first"], resources=["db"])
    results = scheduler.run(executor)
    assert results["first"].state == "SUCCEEDED"
    assert results["second"].state == "SUCCEEDED"  # lock was released


# -- event log ---------------------------------------------------------------------


def test_event_log_is_monotonic_and_complete(tmp_path):
    store = str(tmp_path / "sched.db")

    def executor(task, attempt, control, guard):
        return {"success": True, "output": "ok", "terminal": True}

    scheduler = DAGScheduler(store_path=store, max_workers=2)
    scheduler.add_task("a")
    scheduler.add_task("b", depends_on=["a"])
    scheduler.run(executor)

    events = scheduler.events(limit=100)
    assert events, "expected durable events"
    seqs = [event["seq"] for event in events]
    # Most recent first, strictly decreasing.
    assert seqs == sorted(seqs, reverse=True)
    assert len(set(seqs)) == len(seqs)
    states = {event["state"] for event in events}
    assert "QUEUED" in states
    assert "RUNNING" in states
    assert "SUCCEEDED" in states
    # Every event carries the structured identity fields.
    for event in events:
        assert set(event) >= {"seq", "task_id", "attempt_id",
                              "generation", "state", "detail", "ts"}


def test_events_limit_zero_returns_nothing(tmp_path):
    store = str(tmp_path / "sched.db")
    scheduler = DAGScheduler(store_path=store, max_workers=1)
    scheduler.add_task("a")
    scheduler.run(lambda t, a, c, g: {"success": True,
                                      "output": "", "terminal": True})
    assert scheduler.events(limit=0) == []
    assert scheduler.events(limit=-5) == []


# -- cancellation -------------------------------------------------------------------


def _cancel_executor(started):
    def executor(task, attempt, control, guard):
        started.append(task.task_id)
        time.sleep(0.1)
        return {"success": True, "output": "ok", "terminal": True}
    return executor


def test_cancel_queued_task_cancels_it_and_dependents():
    scheduler = DAGScheduler(max_workers=1, cancel_grace=0.2)
    scheduler.add_task("slow")
    scheduler.add_task("after", depends_on=["slow"])
    results = scheduler.run(_cancel_executor([]))
    # Sanity: without cancel both run.
    assert results["slow"].state == "SUCCEEDED"
    assert results["after"].state == "SUCCEEDED"

    scheduler2 = DAGScheduler(max_workers=1, cancel_grace=0.2)
    scheduler2.add_task("slow")
    scheduler2.add_task("after", depends_on=["slow"])
    started2 = []

    threading.Timer(0.02, lambda: scheduler2.cancel_all()).start()
    results2 = scheduler2.run(_cancel_executor(started2))
    # The running task is cancelled (it never confirms the checkpoint);
    # the queued dependent is cancelled too — nothing is fabricated.
    assert results2["slow"].state == "CANCELLED"
    assert results2["after"].state in ("CANCELLED", "SKIPPED")
    assert "after" not in started2


def test_cancelled_task_is_not_retried():
    calls = []

    def executor(task, attempt, control, guard):
        calls.append(1)
        time.sleep(0.15)
        control.checkpoint("mid")
        return {"success": True, "output": "late", "terminal": True}

    scheduler = DAGScheduler(max_workers=1, cancel_grace=0.5)
    scheduler.add_task("c", max_attempts=3)

    def cancel_later():
        time.sleep(0.03)
        scheduler.cancel("c")

    threading.Timer(0.03, cancel_later).start()
    results = scheduler.run(executor)
    assert results["c"].state == "CANCELLED"
    # One attempt was started; cancellation is terminal (no retry loop).
    assert results["c"].attempts == 1
