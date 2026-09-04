from __future__ import annotations

from forge.core.agent_pipeline import AgentPipeline
from forge.core.task_engine import Task, TaskStatus


def make_task() -> Task:
    return Task(
        id="task-1",
        description="Build feature",
    )


def test_pipeline_runs_all_stages() -> None:
    stages = []

    def handler(task, stage):
        stages.append(stage)
        return f"{stage.value} complete"

    task = make_task()
    pipeline = AgentPipeline(handler)

    results = pipeline.execute(task)

    assert stages == [
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.TESTING,
        TaskStatus.REVIEWING,
    ]
    assert len(results) == 4
    assert all(result.success for result in results)
    assert task.status == TaskStatus.COMPLETED


def test_pipeline_preserves_stage_output() -> None:
    def handler(task, stage):
        return f"output-{stage.value}"

    results = AgentPipeline(handler).execute(make_task())

    assert [result.output for result in results] == [
        "output-planning",
        "output-coding",
        "output-testing",
        "output-reviewing",
    ]


def test_pipeline_stops_on_stage_failure() -> None:
    executed = []

    def handler(task, stage):
        executed.append(stage)

        if stage == TaskStatus.TESTING:
            raise RuntimeError("tests failed")

        return "ok"

    task = make_task()
    results = AgentPipeline(handler).execute(task)

    assert executed == [
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.TESTING,
    ]

    assert len(results) == 3
    assert results[-1].stage == TaskStatus.TESTING
    assert not results[-1].success
    assert results[-1].error == "tests failed"
    assert task.status == TaskStatus.FAILED


def test_pipeline_does_not_run_after_failure() -> None:
    executed = []

    def handler(task, stage):
        executed.append(stage)

        if stage == TaskStatus.CODING:
            raise RuntimeError("coding failed")

        return "ok"

    task = make_task()
    AgentPipeline(handler).execute(task)

    assert executed == [
        TaskStatus.PLANNING,
        TaskStatus.CODING,
    ]


def test_pipeline_converts_output_to_string() -> None:
    def handler(task, stage):
        return 123

    results = AgentPipeline(handler).execute(make_task())

    assert all(result.output == "123" for result in results)


def test_pipeline_handles_empty_string_output() -> None:
    results = AgentPipeline(
        lambda task, stage: "",
    ).execute(make_task())

    assert len(results) == 4
    assert all(result.success for result in results)
