from forge.core.task_engine import TaskEngine, TaskStatus


def test_task_starts_pending():
    engine = TaskEngine()
    task = engine.add("1", "build feature")

    assert task.status == TaskStatus.PENDING


def test_task_can_move_through_lifecycle():
    engine = TaskEngine()
    engine.add("1", "build feature")

    lifecycle = [
        TaskStatus.PLANNING,
        TaskStatus.RESEARCHING,
        TaskStatus.CODING,
        TaskStatus.TESTING,
        TaskStatus.DEBUGGING,
        TaskStatus.REVIEWING,
        TaskStatus.COMPLETED,
    ]

    for status in lifecycle:
        task = engine.set_status("1", status)
        assert task.status == status


def test_recovery_status_supported():
    engine = TaskEngine()
    engine.add("1", "recover task")

    task = engine.set_status("1", TaskStatus.RECOVERY)

    assert task.status == TaskStatus.RECOVERY


def test_existing_start_still_works():
    engine = TaskEngine()
    engine.add("1", "build feature")

    task = engine.start("1")

    assert task.status == TaskStatus.RUNNING
    assert task.attempts == 1


def test_existing_failure_still_works():
    engine = TaskEngine()
    engine.add("1", "build feature")

    task = engine.fail("1", "test failure")

    assert task.status == TaskStatus.FAILED
    assert task.errors == ["test failure"]
