from forge.core.task_engine import TaskEngine, TaskStatus


def test_task_lifecycle():
    engine = TaskEngine()

    task = engine.add("T001", "Create first Forge module")

    assert task.status == TaskStatus.PENDING

    engine.start("T001")

    assert task.status == TaskStatus.RUNNING
    assert task.attempts == 1

    engine.complete("T001")

    assert task.status == TaskStatus.COMPLETED
