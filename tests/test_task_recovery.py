from __future__ import annotations

import pytest

from forge.core.task_engine import Task, TaskStatus
from forge.core.task_recovery import (
    RecoveryPolicy,
    TaskRecoveryEngine,
)
from forge.core.task_store import TaskStore


def test_running_task_is_recovered_after_restart(tmp_path) -> None:
    database = tmp_path / "tasks.db"
    store = TaskStore(database)

    store.save(
        Task(
            id="task-1",
            description="Build",
            status=TaskStatus.RUNNING,
            attempts=1,
        )
    )

    recovery = TaskRecoveryEngine(store)

    recovered = recovery.recover_interrupted()

    assert [task.id for task in recovered] == ["task-1"]

    loaded = store.load("task-1")

    assert loaded.status == TaskStatus.RECOVERY
    assert loaded.attempts == 1
    assert "interrupted" in loaded.errors[-1]


def test_failed_task_can_be_retried(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    store.save(
        Task(
            id="task-1",
            description="Build",
            status=TaskStatus.FAILED,
            attempts=1,
            errors=["build failed"],
        )
    )

    recovery = TaskRecoveryEngine(store)

    recovered = recovery.recover_failed()

    assert [task.id for task in recovered] == ["task-1"]
    assert store.load("task-1").status == TaskStatus.PENDING


def test_max_attempts_prevents_retry(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    task = Task(
        id="task-1",
        description="Build",
        status=TaskStatus.FAILED,
        attempts=3,
    )
    store.save(task)

    recovery = TaskRecoveryEngine(
        store,
        RecoveryPolicy(max_attempts=3),
    )

    assert recovery.recover_failed() == []
    assert store.load("task-1").status == TaskStatus.FAILED


def test_retry_policy_can_disable_failed_retries(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    store.save(
        Task(
            id="task-1",
            description="Build",
            status=TaskStatus.FAILED,
            attempts=1,
        )
    )

    recovery = TaskRecoveryEngine(
        store,
        RecoveryPolicy(retry_failed=False),
    )

    assert recovery.recover_failed() == []
    assert store.load("task-1").status == TaskStatus.FAILED


def test_retryable_task_respects_attempt_limit(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    recovery = TaskRecoveryEngine(
        store,
        RecoveryPolicy(max_attempts=3),
    )

    assert recovery.retryable(
        Task(
            "a",
            "A",
            status=TaskStatus.PENDING,
            attempts=0,
        )
    )

    assert not recovery.retryable(
        Task(
            "b",
            "B",
            status=TaskStatus.FAILED,
            attempts=3,
        )
    )


def test_mark_recovery_persists_reason(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    task = Task("task-1", "Build")
    store.save(task)

    recovery = TaskRecoveryEngine(store)

    recovery.mark_recovery(task, "Worker crashed.")

    loaded = store.load("task-1")

    assert loaded.status == TaskStatus.RECOVERY
    assert loaded.errors[-1] == "Worker crashed."


def test_mark_retry_returns_task_to_pending(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    task = Task(
        "task-1",
        "Build",
        status=TaskStatus.RECOVERY,
        attempts=1,
    )
    store.save(task)

    recovery = TaskRecoveryEngine(store)

    result = recovery.mark_retry(task)

    assert result.status == TaskStatus.PENDING
    assert store.load("task-1").status == TaskStatus.PENDING


def test_mark_retry_rejects_exhausted_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    task = Task(
        "task-1",
        "Build",
        status=TaskStatus.FAILED,
        attempts=3,
    )
    store.save(task)

    recovery = TaskRecoveryEngine(
        store,
        RecoveryPolicy(max_attempts=3),
    )

    with pytest.raises(
        RuntimeError,
        match="Task cannot be retried",
    ):
        recovery.mark_retry(task)


def test_complete_recovery_pass(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    store.save(
        Task(
            "running",
            "Running task",
            status=TaskStatus.RUNNING,
            attempts=1,
        )
    )

    store.save(
        Task(
            "failed",
            "Failed task",
            status=TaskStatus.FAILED,
            attempts=1,
        )
    )

    store.save(
        Task(
            "done",
            "Completed task",
            status=TaskStatus.COMPLETED,
        )
    )

    recovery = TaskRecoveryEngine(store)

    recovered = recovery.recover()

    assert [task.id for task in recovered] == [
        "running",
        "failed",
    ]

    assert store.load("running").status == TaskStatus.RECOVERY
    assert store.load("failed").status == TaskStatus.PENDING
    assert store.load("done").status == TaskStatus.COMPLETED
