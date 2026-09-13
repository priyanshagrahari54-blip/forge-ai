"""Execution fencing tests (Session 10, Section 2.4/2.6).

These are the core safety properties of the scheduler: a timed-out or
cancelled worker must not be able to modify shared state after Forge has
marked its attempt abandoned — its late result is rejected, its guard
refuses further writes, and a retry never overlaps the abandoned
attempt.
"""
from __future__ import annotations

import threading
import time

import pytest

from forge.core.dag_scheduler import DAGScheduler
from forge.core.fencing import (
    AttemptFence,
    FenceRegistry,
    StaleAttemptError,
    commit_guard,
)


# -- fence registry unit behavior -------------------------------------------


def test_fence_states_progress_forward_only():
    registry = FenceRegistry()
    fence = registry.begin("t1")
    fence = registry.mark_running("t1", fence)
    fence = registry.fence("t1", fence, reason="timeout")
    assert fence.state == "FENCED"
    assert registry.current("t1").state == "FENCED"
    # Terminal: no further transitions.
    with pytest.raises(Exception):
        registry.mark_running("t1", fence)


def test_stale_commit_is_rejected():
    registry = FenceRegistry()
    fence = registry.begin("t1")
    fence = registry.mark_running("t1", fence)
    fence = registry.fence("t1", fence, reason="timeout")

    with pytest.raises(StaleAttemptError):
        registry.commit("t1", fence, "SUCCEEDED", payload="result")


def test_retry_gets_higher_generation_and_cannot_overlap():
    registry = FenceRegistry()
    first = registry.begin("t1")
    registry.mark_running("t1", first)
    registry.fence("t1", first, reason="superseded")
    second = registry.begin("t1")
    assert second.generation > first.generation
    # The old fence can no longer authorize anything — even though the
    # caller's (stale) copy still claims to be running.
    assert commit_guard(first, registry)() != ""
    assert commit_guard(second, registry)() == ""
    # And the old fence cannot commit.
    with pytest.raises(StaleAttemptError):
        registry.commit("t1", first, "SUCCEEDED", payload="stale")
    record = registry.commit("t1", second, "SUCCEEDED", payload="fresh")
    assert record["generation"] == second.generation


def test_guard_refuses_writes_on_fenced_fence():
    registry = FenceRegistry()
    fence = registry.begin("t1")
    fence = registry.mark_running("t1", fence)
    assert commit_guard(fence, registry)() == ""
    registry.fence("t1", fence, reason="timeout")
    reason = commit_guard(fence, registry)()
    assert reason  # non-empty => refused


def test_seed_generation_restores_high_water_mark():
    """A fresh process must continue the persisted generation counter:
    reusing the old process's fenced generation would let a stale
    attempt masquerade as current."""
    old = FenceRegistry()
    fence = old.begin("t1")
    old.mark_running("t1", fence)
    old.fence("t1", fence, reason="timeout")
    assert old.generation_of("t1") == 1

    fresh = FenceRegistry()
    fresh.seed_generation("t1", 1)
    next_fence = fresh.begin("t1")
    assert next_fence.generation == 2  # never reuses fenced gen 1
    # Seeding can only raise, never lower.
    fresh.seed_generation("t1", 0)
    assert fresh.generation_of("t1") == 2


# -- scheduler-level fencing ----------------------------------------------------


def test_timeout_fences_worker_and_rejects_its_late_result():
    """A worker that runs past its deadline is fenced at the deadline;
    when it finally returns a 'success', that result is rejected as
    stale and the task ends FENCED — never SUCCEEDED with late output."""
    def executor(task, attempt, control, guard):
        time.sleep(0.3)  # past the 0.1s deadline, then 'succeeds' late
        return {"success": True, "output": "late", "terminal": True}

    scheduler = DAGScheduler(max_workers=2, abandon_wait=0.5)
    scheduler.add_task("hung", timeout=0.1, max_attempts=1)
    results = scheduler.run(executor)

    # The late success must not have landed.
    assert results["hung"].state == "FENCED"
    assert results["hung"].output != "late"
    assert not results["hung"].succeeded


def test_late_worker_cannot_write_through_guard():
    """After the deadline, the guard refuses further writes for the
    abandoned attempt — the change-set choke point sees a non-empty
    reason for every attempted write."""
    guard_state = {"refusals": 0, "authorized": 0}

    def executor(task, attempt, control, guard):
        time.sleep(0.3)  # pass the 0.1s deadline while 'working'
        # Try to 'write' after the fence.
        for _ in range(3):
            reason = guard()
            if reason:
                guard_state["refusals"] += 1
            else:
                guard_state["authorized"] += 1
        return {"success": True, "output": "should be stale",
                "terminal": True}

    scheduler = DAGScheduler(max_workers=2, abandon_wait=0.5)
    scheduler.add_task("late", timeout=0.1, max_attempts=1)
    results = scheduler.run(executor)

    assert results["late"].state == "FENCED"
    assert guard_state["refusals"] == 3
    assert guard_state["authorized"] == 0
    assert "timed out" in results["late"].error


def test_retry_never_overlaps_abandoned_attempt():
    """Attempt 1 runs past its deadline and is fenced. It then returns a
    'success', which is rejected as stale. Only after that thread has
    finished is attempt 2 dispatched — so the two attempts never run at
    the same time, and the success belongs to generation 2, not the
    abandoned generation 1."""
    active = 0
    peak_concurrency = 0
    overlap_lock = threading.Lock()
    generations = []

    def executor(task, attempt, control, guard):
        nonlocal active, peak_concurrency
        with overlap_lock:
            active += 1
            peak_concurrency = max(peak_concurrency, active)
            generations.append(attempt.generation)
        try:
            if attempt.generation == 1:
                time.sleep(0.3)  # past the 0.1s deadline; result is stale
                return {"success": True, "output": "stale", "terminal": True}
            return {"success": True, "output": "fresh", "terminal": True}
        finally:
            with overlap_lock:
                active -= 1

    scheduler = DAGScheduler(max_workers=2, abandon_wait=1.0)
    scheduler.add_task("flaky-hang", timeout=0.1, max_attempts=2)
    results = scheduler.run(executor)

    assert results["flaky-hang"].state == "SUCCEEDED"
    assert results["flaky-hang"].output == "fresh"
    assert results["flaky-hang"].attempts == 2
    assert generations == [1, 2]
    # No moment where both attempts were executing.
    assert peak_concurrency == 1, peak_concurrency


def test_cancel_fences_and_late_result_is_rejected():
    def executor(task, attempt, control, guard):
        time.sleep(1.0)
        return {"success": True, "output": "late", "terminal": True}

    scheduler = DAGScheduler(max_workers=2, cancel_grace=0.2, abandon_wait=0.3)
    scheduler.add_task("c", timeout=5.0, max_attempts=1)

    threading.Timer(0.1, lambda: scheduler.cancel("c")).start()
    results = scheduler.run(executor)

    assert results["c"].state == "CANCELLED"
    assert results["c"].output != "late"


def test_events_are_durable_across_restarts(tmp_path):
    """Terminal states and the event sequence survive a process
    restart; a reopened store cannot resurrect a finished task."""
    store = str(tmp_path / "sched.db")

    def executor(task, attempt, control, guard):
        return {"success": True, "output": "ok", "terminal": True}

    scheduler = DAGScheduler(store_path=store, max_workers=1)
    scheduler.add_task("done")
    scheduler.run(executor)
    before = [event["seq"] for event in scheduler.events(limit=100)]

    reopened = DAGScheduler(store_path=store, max_workers=1)
    results = reopened.run(executor)
    assert results["done"].state == "SUCCEEDED"
    assert results["done"].attempts == 1  # adopted, not re-executed
    after = [event["seq"] for event in reopened.events(limit=100)]
    # Durable, monotonic, and extending (no reuse).
    assert set(before) <= set(after)
    assert after == sorted(after, reverse=True)
    assert len(set(after)) == len(after)


def test_failed_task_persists_and_is_not_re_executed(tmp_path):
    store = str(tmp_path / "sched.db")
    calls = []

    def executor(task, attempt, control, guard):
        calls.append(task.task_id)
        return {"success": False, "error": "fatal", "terminal": True}

    scheduler = DAGScheduler(store_path=store, max_workers=1)
    scheduler.add_task("doomed")
    scheduler.run(executor)
    assert calls == ["doomed"]

    reopened = DAGScheduler(store_path=store, max_workers=1)
    results = reopened.run(executor)
    assert results["doomed"].state == "FAILED"
    assert "fatal" in results["doomed"].error
    assert calls == ["doomed"]  # terminal state persisted; no re-run
