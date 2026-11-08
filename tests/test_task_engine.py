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


def test_task_engine_external_mutation_sync():
    from forge.core.task_engine import Task

    engine = TaskEngine()
    engine.add("T001", "Task 1")

    # Mutate tasks directly (as PersistentTaskQueue or other components do)
    t2 = Task(id="T002", description="Task 2")
    engine.tasks.append(t2)

    assert engine._exists("T002")
    assert engine._find("T002") is t2

    # In-place replace
    t2_replaced = Task(id="T002_NEW", description="Task 2 Replaced")
    engine.tasks[1] = t2_replaced

    assert engine._exists("T002_NEW")
    assert not engine._exists("T002")
    assert engine._find("T002_NEW") is t2_replaced

    # Entire list reassignment
    engine.tasks = [Task(id="T003", description="Task 3")]
    assert engine._exists("T003")
    assert not engine._exists("T001")
    assert engine._find("T003").description == "Task 3"
