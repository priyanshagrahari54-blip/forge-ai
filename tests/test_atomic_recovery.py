from pathlib import Path

from forge.core.task_engine import TaskStatus
from forge.core.task_recovery import TaskRecoveryEngine
from forge.core.task_store import TaskStore
from forge.core.task_queue import PersistentTaskQueue


def test_recover_interrupted_claims_each_running_task_once(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    queue = PersistentTaskQueue(store=store)
    queue.add("task-1", "interrupted")
    assert queue.start_next() is not None

    first = TaskRecoveryEngine(TaskStore(path)).recover_interrupted()
    second = TaskRecoveryEngine(TaskStore(path)).recover_interrupted()

    assert [item.id for item in first] == ["task-1"]
    assert second == []
    recovered = TaskStore(path).load("task-1")
    assert recovered.status == TaskStatus.RECOVERY
    assert recovered.errors.count("Task interrupted and moved to recovery.") == 1
