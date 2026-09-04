import pytest

from forge.core.task_engine import TaskEngine, TaskStatus


def test_task_without_dependencies_can_start():
    engine = TaskEngine()

    engine.add("a", "First task")

    assert engine.can_start("a") is True

    task = engine.start("a")

    assert task.status == TaskStatus.RUNNING


def test_dependent_task_cannot_start_before_dependency():
    engine = TaskEngine()

    engine.add("a", "First task")
    engine.add("b", "Second task", dependencies=["a"])

    assert engine.can_start("b") is False

    with pytest.raises(RuntimeError):
        engine.start("b")

    assert engine._find("b").status == TaskStatus.PENDING


def test_dependent_task_can_start_after_dependency_completes():
    engine = TaskEngine()

    engine.add("a", "First task")
    engine.add("b", "Second task", dependencies=["a"])

    engine.start("a")
    engine.complete("a")

    assert engine.can_start("b") is True

    task = engine.start("b")

    assert task.status == TaskStatus.RUNNING


def test_multiple_dependencies_must_all_complete():
    engine = TaskEngine()

    engine.add("a", "First task")
    engine.add("b", "Second task")
    engine.add("c", "Third task", dependencies=["a", "b"])

    engine.start("a")
    engine.complete("a")

    assert engine.can_start("c") is False

    engine.start("b")
    engine.complete("b")

    assert engine.can_start("c") is True


def test_missing_dependency_is_rejected():
    engine = TaskEngine()

    with pytest.raises(KeyError):
        engine.add(
            "b",
            "Second task",
            dependencies=["missing"],
        )


def test_self_dependency_is_rejected():
    engine = TaskEngine()

    with pytest.raises(ValueError):
        engine.add(
            "a",
            "Task",
            dependencies=["a"],
        )


def test_add_dependency():
    engine = TaskEngine()

    engine.add("a", "First task")
    engine.add("b", "Second task")

    task = engine.add_dependency("b", "a")

    assert task.dependencies == ["a"]
    assert engine.can_start("b") is False


def test_existing_task_api_still_works():
    engine = TaskEngine()

    task = engine.add("a", "Existing behavior")

    assert task.dependencies == []
    assert task.status == TaskStatus.PENDING

    engine.start("a")
    engine.complete("a")

    assert task.status == TaskStatus.COMPLETED
    assert task.attempts == 1
