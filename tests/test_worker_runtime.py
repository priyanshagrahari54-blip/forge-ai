from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from forge.core.task_engine import TaskStatus
from forge.core.task_queue import PersistentTaskQueue
from forge.core.task_recovery import TaskRecoveryEngine
from forge.core.task_store import TaskStore
from forge.core.worker_runtime import PersistentWorkerRuntime


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def make_runtime(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    recovery = TaskRecoveryEngine(store)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runtime = PersistentWorkerRuntime(
        queue,
        recovery,
        lease_timeout=timedelta(seconds=30),
        now=clock.now,
    )
    return queue, store, recovery, clock, runtime


def test_claim_next_starts_and_leases_task(tmp_path):
    queue, store, _, _, runtime = make_runtime(tmp_path)
    queue.add("task-1", "build")

    task = runtime.claim_next("worker-a")

    assert task is not None
    assert task.id == "task-1"
    assert task.status == TaskStatus.RUNNING
    assert runtime.owner_of("task-1") == "worker-a"
    assert store.load("task-1").status == TaskStatus.RUNNING


def test_claim_next_returns_none_when_idle(tmp_path):
    _, _, _, _, runtime = make_runtime(tmp_path)

    assert runtime.claim_next("worker-a") is None


def test_heartbeat_updates_lease_and_validates_owner(tmp_path):
    queue, _, _, clock, runtime = make_runtime(tmp_path)
    queue.add("task-1", "build")
    runtime.claim_next("worker-a")

    first = runtime.active_leases()[0]
    clock.advance(10)

    assert runtime.heartbeat("task-1", "worker-a")

    second = runtime.active_leases()[0]
    assert second.heartbeat_at > first.heartbeat_at
    assert not runtime.heartbeat("task-1", "worker-b")


def test_complete_requires_owner_and_releases_lease(tmp_path):
    queue, store, _, _, runtime = make_runtime(tmp_path)
    queue.add("task-1", "build")
    runtime.claim_next("worker-a")

    with pytest.raises(RuntimeError, match="leased by"):
        runtime.complete("task-1", "worker-b")

    completed = runtime.complete("task-1", "worker-a")

    assert completed.status == TaskStatus.COMPLETED
    assert runtime.active_leases() == ()
    assert store.load("task-1").status == TaskStatus.COMPLETED


def test_fail_requires_owner_and_releases_lease(tmp_path):
    queue, store, _, _, runtime = make_runtime(tmp_path)
    queue.add("task-1", "build")
    runtime.claim_next("worker-a")

    failed = runtime.fail("task-1", "worker-a", "boom")

    assert failed.status == TaskStatus.FAILED
    assert failed.errors[-1] == "boom"
    assert runtime.active_leases() == ()
    assert store.load("task-1").status == TaskStatus.FAILED


def test_release_moves_task_to_recovery(tmp_path):
    queue, store, _, _, runtime = make_runtime(tmp_path)
    queue.add("task-1", "build")
    runtime.claim_next("worker-a")

    recovered = runtime.release("task-1", "worker-a", "worker drained")

    assert recovered.status == TaskStatus.RECOVERY
    assert recovered.errors[-1] == "worker drained"
    assert runtime.active_leases() == ()
    assert store.load("task-1").status == TaskStatus.RECOVERY


def test_recover_stale_leases_marks_recovery(tmp_path):
    queue, store, _, clock, runtime = make_runtime(tmp_path)
    queue.add("task-1", "build")
    runtime.claim_next("worker-a")

    clock.advance(31)

    recovered = runtime.recover_stale_leases()

    assert [task.id for task in recovered] == ["task-1"]
    assert recovered[0].status == TaskStatus.RECOVERY
    assert "lease expired" in recovered[0].errors[-1]
    assert runtime.active_leases() == ()
    assert store.load("task-1").status == TaskStatus.RECOVERY


def test_recover_stale_leases_keeps_fresh_lease(tmp_path):
    queue, _, _, clock, runtime = make_runtime(tmp_path)
    queue.add("task-1", "build")
    runtime.claim_next("worker-a")

    clock.advance(29)

    recovered = runtime.recover_stale_leases()

    assert recovered == []
    assert len(runtime.active_leases()) == 1
