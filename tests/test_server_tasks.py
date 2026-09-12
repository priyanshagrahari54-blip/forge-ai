"""Task management unit tests (A81): lifecycle, fields, persistence.

Covers the contract every Forge task must satisfy — task_id, project_id,
status, timestamps, checkpoint, current stage, retry count, result, and
error — plus the closed transition table and durable persistence across
database reopens.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from forge.server.errors import (  # noqa: E402
    InvalidRequest,
    InvalidTransition,
    TaskNotFound,
    VersionConflict,
)
from forge.server.models import (  # noqa: E402
    TRANSITIONS,
    TaskStatus,
    can_transition,
)
from forge.server.storage import Database  # noqa: E402
from forge.server.tasks import TaskManager  # noqa: E402


def make_manager(tmp_path) -> TaskManager:
    return TaskManager(Database(tmp_path / "server.db"))


# -- lifecycle states -----------------------------------------------------------

def test_all_ten_lifecycle_states_exist():
    values = {status.value for status in TaskStatus}
    assert values == {
        "created", "queued", "started", "paused", "waiting_for_approval",
        "running", "completed", "failed", "cancelled", "rolled_back"}


def test_full_happy_path_transitions(tmp_path):
    tasks = make_manager(tmp_path)
    task = tasks.create("demo", "Build a feature")
    assert task.status == TaskStatus.CREATED

    task = tasks.transition(task.task_id, TaskStatus.QUEUED)
    assert task.status == TaskStatus.QUEUED
    assert task.queued_at is not None

    task = tasks.transition(task.task_id, TaskStatus.STARTED)
    assert task.started_at is not None

    task = tasks.transition(task.task_id, TaskStatus.RUNNING)
    task = tasks.transition(task.task_id, TaskStatus.COMPLETED,
                            result_json='{"ok": true}', stage="completed",
                            progress=1.0)
    assert task.status == TaskStatus.COMPLETED
    assert task.finished_at is not None
    assert task.result() == {"ok": True}
    assert task.terminal


def test_approval_and_pause_paths(tmp_path):
    tasks = make_manager(tmp_path)
    task = tasks.create("demo", "Build")
    tasks.transition(task.task_id, TaskStatus.QUEUED)
    tasks.transition(task.task_id, TaskStatus.STARTED)
    tasks.transition(task.task_id, TaskStatus.RUNNING)

    task = tasks.transition(task.task_id, TaskStatus.PAUSED)
    assert task.status == TaskStatus.PAUSED
    task = tasks.transition(task.task_id, TaskStatus.RUNNING)

    task = tasks.transition(task.task_id, TaskStatus.WAITING_FOR_APPROVAL)
    assert task.status == TaskStatus.WAITING_FOR_APPROVAL
    task = tasks.transition(task.task_id, TaskStatus.RUNNING)

    task = tasks.transition(task.task_id, TaskStatus.CANCELLED,
                            error="Cancelled by operator.")
    assert task.status == TaskStatus.CANCELLED
    assert task.error == "Cancelled by operator."
    # Manual retry from cancelled is legal; from rolled_back it is not.
    task = tasks.transition(task.task_id, TaskStatus.QUEUED)
    assert task.status == TaskStatus.QUEUED


def test_rollback_path_from_completed_and_failed(tmp_path):
    tasks = make_manager(tmp_path)
    for final in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        task = tasks.create("demo", "Build")
        tasks.transition(task.task_id, TaskStatus.QUEUED)
        tasks.transition(task.task_id, TaskStatus.STARTED)
        tasks.transition(task.task_id, TaskStatus.RUNNING)
        tasks.transition(task.task_id, final)
        rolled = tasks.transition(task.task_id, TaskStatus.ROLLED_BACK)
        assert rolled.status == TaskStatus.ROLLED_BACK
        assert TRANSITIONS[TaskStatus.ROLLED_BACK] == frozenset()


def test_illegal_transitions_refused(tmp_path):
    tasks = make_manager(tmp_path)
    task = tasks.create("demo", "Build")
    with pytest.raises(InvalidTransition):
        tasks.transition(task.task_id, TaskStatus.RUNNING)
    with pytest.raises(InvalidTransition):
        tasks.transition(task.task_id, TaskStatus.COMPLETED)
    tasks.transition(task.task_id, TaskStatus.QUEUED)
    with pytest.raises(InvalidTransition):
        tasks.transition(task.task_id, TaskStatus.COMPLETED)


def test_transition_table_is_closed_and_complete():
    # Every status appears as a key; terminal states only where declared.
    assert set(TRANSITIONS) == set(TaskStatus)
    assert can_transition(TaskStatus.CREATED, TaskStatus.QUEUED)
    assert not can_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
    assert not can_transition(TaskStatus.ROLLED_BACK, TaskStatus.QUEUED)


# -- expected-status / version fencing --------------------------------------------

def test_expected_status_narrows_source(tmp_path):
    tasks = make_manager(tmp_path)
    task = tasks.create("demo", "Build")
    tasks.transition(task.task_id, TaskStatus.QUEUED)
    with pytest.raises(InvalidTransition):
        tasks.transition(task.task_id, TaskStatus.STARTED,
                         expected=(TaskStatus.RUNNING,))
    started = tasks.transition(task.task_id, TaskStatus.STARTED,
                               expected=(TaskStatus.QUEUED,))
    assert started.status == TaskStatus.STARTED


def test_expected_version_conflict(tmp_path):
    tasks = make_manager(tmp_path)
    task = tasks.create("demo", "Build")
    with pytest.raises(VersionConflict):
        tasks.transition(task.task_id, TaskStatus.QUEUED,
                         expected_version=task.version + 5)
    moved = tasks.transition(task.task_id, TaskStatus.QUEUED,
                             expected_version=task.version)
    assert moved.version == task.version + 1


# -- required task fields -------------------------------------------------------------

def test_task_carries_every_required_field(tmp_path):
    tasks = make_manager(tmp_path)
    task = tasks.create("demo", "Build a feature", priority=3,
                        mode="autonomous", actor="tester", max_retries=4)
    tasks.transition(task.task_id, TaskStatus.QUEUED)
    tasks.transition(task.task_id, TaskStatus.STARTED)
    tasks.update(task.task_id, checkpoint_id="cp-1", stage="coding",
                 progress=0.3)
    task = tasks.transition(task.task_id, TaskStatus.RUNNING)
    task = tasks.transition(task.task_id, TaskStatus.COMPLETED,
                            result_json='{"files": ["a.py"]}')
    payload = task.to_dict(include_result=True)
    # The contract fields, by name:
    assert payload["task_id"] == task.task_id
    assert payload["project_id"] == "demo"
    assert payload["status"] == "completed"
    assert payload["checkpoint"] == "cp-1"
    assert payload["checkpoint_id"] == "cp-1"
    assert payload["stage"] == "completed"  # completion overrides stage
    assert payload["retry_count"] == 0
    assert payload["max_retries"] == 4
    assert payload["result"] == {"files": ["a.py"]}
    assert payload["error"] == ""
    stamps = payload["timestamps"]
    for key in ("created_at", "queued_at", "started_at", "updated_at",
                "finished_at"):
        assert stamps[key] is not None, key
        assert payload[key] == stamps[key]
    assert payload["priority"] == 3
    assert payload["mode"] == "autonomous"
    assert payload["actor"] == "tester"


def test_update_whitelist_rejects_unknown_fields(tmp_path):
    tasks = make_manager(tmp_path)
    task = tasks.create("demo", "Build")
    with pytest.raises(InvalidRequest):
        tasks.update(task.task_id, requirement="smuggled", status="x")
    with pytest.raises(InvalidRequest):
        tasks.update(task.task_id, bogus_field=1)
    updated = tasks.update(task.task_id, stage="planning", progress=0.1)
    assert updated.stage == "planning"


def test_create_validation_fails_closed(tmp_path):
    tasks = make_manager(tmp_path)
    with pytest.raises(InvalidRequest):
        tasks.create("../evil", "requirement")
    with pytest.raises(InvalidRequest):
        tasks.create("demo", "")
    with pytest.raises(InvalidRequest):
        tasks.create("demo", "x" * 8001)
    with pytest.raises(InvalidRequest):
        tasks.create("demo", "nul\x00byte")
    with pytest.raises(InvalidRequest):
        tasks.create("demo", "ok", max_retries=99)


def test_get_or_raise_unknown_task(tmp_path):
    tasks = make_manager(tmp_path)
    with pytest.raises(TaskNotFound):
        tasks.get_or_raise("task-nope")
    assert tasks.get("task-nope") is None


# -- persistence -------------------------------------------------------------------------

def test_tasks_persist_across_database_reopen(tmp_path):
    db_path = tmp_path / "server.db"
    tasks = TaskManager(Database(db_path))
    task = tasks.create("demo", "Persist me", priority=2, mode="assisted",
                        actor="tester")
    tasks.transition(task.task_id, TaskStatus.QUEUED)
    updated = tasks.update(task.task_id, checkpoint_id="cp-9",
                           stage="queued")

    reopened = TaskManager(Database(db_path))
    loaded = reopened.get_or_raise(task.task_id)
    assert loaded.status == TaskStatus.QUEUED
    assert loaded.requirement == "Persist me"
    assert loaded.priority == 2
    assert loaded.checkpoint_id == "cp-9"
    assert loaded.stage == "queued"
    assert loaded.actor == "tester"
    assert loaded.version == updated.version
    assert loaded.queued_at == updated.queued_at


def test_status_counts_and_listing(tmp_path):
    tasks = make_manager(tmp_path)
    first = tasks.create("demo", "one")
    second = tasks.create("demo", "two")
    tasks.create("other", "three")
    tasks.transition(first.task_id, TaskStatus.QUEUED)
    tasks.transition(second.task_id, TaskStatus.QUEUED)
    tasks.transition(second.task_id, TaskStatus.STARTED)

    counts = tasks.status_counts()
    assert counts["queued"] == 1
    assert counts["started"] == 1
    assert counts["created"] == 1
    assert tasks.status_counts("demo")["created"] == 0
    assert len(tasks.list(project_id="demo")) == 2
    assert len(tasks.list(status="queued")) == 1
    active = tasks.active_tasks()
    assert {task.task_id for task in active} == {first.task_id,
                                                 second.task_id}


# -- restart recovery -----------------------------------------------------------------------

def _drive_to_running(tasks: TaskManager, requirement: str, *,
                      max_retries: int = 2):
    task = tasks.create("demo", requirement, max_retries=max_retries)
    tasks.transition(task.task_id, TaskStatus.QUEUED)
    tasks.transition(task.task_id, TaskStatus.STARTED)
    return tasks.transition(task.task_id, TaskStatus.RUNNING)


def test_recover_interrupted_requeues_within_budget(tmp_path):
    db_path = tmp_path / "server.db"
    tasks = TaskManager(Database(db_path))
    running = _drive_to_running(tasks, "interrupted", max_retries=2)

    # A fresh manager over the same database = a restarted server.
    restarted = TaskManager(Database(db_path))
    recovered = restarted.recover_interrupted()
    assert recovered == [{"task_id": running.task_id, "action": "requeued",
                          "retry_count": 1}]
    task = restarted.get_or_raise(running.task_id)
    assert task.status == TaskStatus.QUEUED
    assert task.retry_count == 1


def test_recover_interrupted_fails_when_budget_exhausted(tmp_path):
    db_path = tmp_path / "server.db"
    tasks = TaskManager(Database(db_path))
    running = _drive_to_running(tasks, "interrupted", max_retries=0)

    restarted = TaskManager(Database(db_path))
    recovered = restarted.recover_interrupted()
    assert recovered[0]["action"] == "failed"
    task = restarted.get_or_raise(running.task_id)
    assert task.status == TaskStatus.FAILED
    assert "restart" in task.error
    assert "retry budget exhausted" in task.error


def test_recovery_leaves_terminal_and_queued_tasks_alone(tmp_path):
    db_path = tmp_path / "server.db"
    tasks = TaskManager(Database(db_path))
    queued = tasks.create("demo", "waiting")
    tasks.transition(queued.task_id, TaskStatus.QUEUED)
    done = tasks.create("demo", "done")
    tasks.transition(done.task_id, TaskStatus.QUEUED)
    tasks.transition(done.task_id, TaskStatus.STARTED)
    tasks.transition(done.task_id, TaskStatus.RUNNING)
    tasks.transition(done.task_id, TaskStatus.COMPLETED)
    paused = tasks.create("demo", "paused")
    tasks.transition(paused.task_id, TaskStatus.QUEUED)
    tasks.transition(paused.task_id, TaskStatus.PAUSED)

    restarted = TaskManager(Database(db_path))
    assert restarted.recover_interrupted() == []
    assert restarted.get_or_raise(queued.task_id).status == \
        TaskStatus.QUEUED
    assert restarted.get_or_raise(done.task_id).status == \
        TaskStatus.COMPLETED
    # Pause is a deliberate operator state: it survives restarts.
    assert restarted.get_or_raise(paused.task_id).status == \
        TaskStatus.PAUSED


def test_waiting_for_approval_is_recovered(tmp_path):
    db_path = tmp_path / "server.db"
    tasks = TaskManager(Database(db_path))
    task = _drive_to_running(tasks, "needs approval", max_retries=1)
    tasks.transition(task.task_id, TaskStatus.WAITING_FOR_APPROVAL)

    restarted = TaskManager(Database(db_path))
    recovered = restarted.recover_interrupted()
    assert recovered[0]["action"] == "requeued"
    assert restarted.get_or_raise(task.task_id).status == TaskStatus.QUEUED
