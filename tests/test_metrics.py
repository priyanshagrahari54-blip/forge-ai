from __future__ import annotations

import pytest

from forge.core.agent_pipeline import AgentPipeline
from forge.core.task_engine import Task, TaskStatus
from forge.performance.metrics import MetricsRecorder


def fixed_clock(timestamps):
    timestamps = iter(timestamps)
    return lambda: next(timestamps)


class FakeTimer:
    """Deterministic timer that advances by a fixed step per call."""

    def __init__(self, step: float = 0.25) -> None:
        self.step = step
        self.value = 0.0

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def test_record_stores_fields():
    recorder = MetricsRecorder(
        clock=fixed_clock(["2026-09-06T00:00:00+00:00"])
    )

    record = recorder.record(
        "task-1",
        "coding",
        agent="coder",
        duration_ms=12.5,
        attempts=2,
        retries=1,
        affected_files=("forge/a.py",),
    )

    assert record.task_id == "task-1"
    assert record.stage == "coding"
    assert record.agent == "coder"
    assert record.status == "success"
    assert record.duration_ms == 12.5
    assert record.attempts == 2
    assert record.retries == 1
    assert record.affected_files == ("forge/a.py",)
    assert record.timestamp == "2026-09-06T00:00:00+00:00"


def test_for_task_and_for_agent():
    recorder = MetricsRecorder(clock=fixed_clock(["t1", "t2", "t3"]))

    recorder.record("task-1", "coding", agent="coder")
    recorder.record("task-1", "testing", agent="tester")
    recorder.record("task-2", "coding", agent="coder")

    assert [r.stage for r in recorder.for_task("task-1")] == [
        "coding",
        "testing",
    ]
    assert [r.task_id for r in recorder.for_agent("coder")] == [
        "task-1",
        "task-2",
    ]


def test_by_stage():
    recorder = MetricsRecorder()

    recorder.record("task-1", "coding")
    recorder.record("task-2", "testing")

    assert len(recorder.by_stage("coding")) == 1
    assert recorder.by_stage("coding")[0].task_id == "task-1"


def test_summary_counts_and_mean_duration():
    recorder = MetricsRecorder()

    recorder.record("task-1", "coding", agent="coder", duration_ms=10.0)
    recorder.record(
        "task-2",
        "testing",
        agent="tester",
        duration_ms=30.0,
    )
    recorder.record(
        "task-3",
        "coding",
        agent="coder",
        status="failure",
        duration_ms=20.0,
    )

    summary = recorder.summary()

    assert summary["count"] == 3
    assert summary["successes"] == 2
    assert summary["failures"] == 1
    assert summary["total_duration_ms"] == 60.0
    assert summary["mean_duration_ms"] == 20.0
    assert summary["by_status"] == {
        "success": 2,
        "failure": 1,
    }
    assert summary["by_stage"] == {
        "coding": 2,
        "testing": 1,
    }
    assert summary["mean_duration_ms_by_agent"]["coder"] == 15.0
    assert summary["mean_duration_ms_by_agent"]["tester"] == 30.0


def test_to_dict_and_clear():
    recorder = MetricsRecorder()

    recorder.record("task-1", "coding")

    data = recorder.to_dict()

    assert len(data["records"]) == 1
    assert data["records"][0]["task_id"] == "task-1"

    recorder.clear()

    assert recorder.records == ()


def test_max_records_is_bounded():
    recorder = MetricsRecorder(max_records=3)

    for index in range(10):
        recorder.record(f"task-{index}", "coding")

    assert len(recorder.records) == 3
    assert [r.task_id for r in recorder.records] == [
        "task-7",
        "task-8",
        "task-9",
    ]
def make_registry():
    from forge.agents.execution import CallableAgentExecutor
    from forge.agents.registry import AgentRegistration, AgentRegistry

    registry = AgentRegistry()

    for name, role, capability in (
        ("debugger", "debugging", "debugging"),
        ("coder", "coding", "coding"),
        ("tester", "testing", "testing"),
        ("reviewer", "reviewing", "review"),
    ):
        registry.register(
            AgentRegistration(
                name=name,
                role=role,
                capabilities=(capability,),
                executor=CallableAgentExecutor(
                    name,
                    lambda request: f"{name} output",
                ),
            )
        )

    return registry


def test_pipeline_records_stage_metrics():
    from forge.agents.planner import CapabilityAgentPlanner

    registry = make_registry()
    plan = CapabilityAgentPlanner(registry).plan(
        "Fix the bug, implement the code, run tests, and review the code."
    )

    recorder = MetricsRecorder()
    pipeline = AgentPipeline(
        agent_registry=registry,
        metrics_recorder=recorder,
        timer=FakeTimer(step=0.25),
    )

    task = Task(
        id="metrics-plan-1",
        description="Fix the bug, implement the code, run tests, and review the code.",
    )

    results = pipeline.execute_plan(task, plan)

    assert all(result.success for result in results)

    records = recorder.for_task("metrics-plan-1")

    assert len(records) == 4
    assert [record.agent for record in records] == [
        "debugger",
        "coder",
        "tester",
        "reviewer",
    ]
    assert [record.stage for record in records] == [
        "debugging",
        "coding",
        "testing",
        "reviewing",
    ]
    assert all(record.status == "success" for record in records)
    assert all(
        record.duration_ms == pytest.approx(250.0)
        for record in records
    )


def test_pipeline_records_failure_metric():
    from forge.agents.execution import CallableAgentExecutor
    from forge.agents.planner import CapabilityAgentPlanner
    from forge.agents.registry import AgentRegistration, AgentRegistry

    registry = AgentRegistry()

    def boom(request):
        raise RuntimeError("coding failed")

    registry.register(
        AgentRegistration(
            name="coder",
            role="coding",
            capabilities=("coding",),
            executor=CallableAgentExecutor("coder", boom),
        )
    )

    plan = CapabilityAgentPlanner(registry).plan("implement the code")

    recorder = MetricsRecorder()
    pipeline = AgentPipeline(
        agent_registry=registry,
        metrics_recorder=recorder,
        timer=FakeTimer(step=0.5),
    )

    task = Task(id="metrics-fail-1", description="implement the code")

    results = pipeline.execute_plan(task, plan)

    assert not results[0].success

    records = recorder.for_task("metrics-fail-1")

    assert len(records) == 1
    assert records[0].status == "failure"
    assert records[0].agent == "coder"
    assert records[0].duration_ms == pytest.approx(500.0)


def test_standard_execute_records_metrics():
    recorder = MetricsRecorder()

    pipeline = AgentPipeline(
        stage_handler=lambda task, stage: "output",
        metrics_recorder=recorder,
        timer=FakeTimer(step=0.1),
    )

    task = Task(id="metrics-exec-1", description="run standard pipeline")

    pipeline.execute(task)

    records = recorder.for_task("metrics-exec-1")

    assert len(records) == 4
    assert [record.stage for record in records] == [
        "planning",
        "coding",
        "testing",
        "reviewing",
    ]
    assert all(
        record.duration_ms == pytest.approx(100.0)
        for record in records
    )


def test_coordinator_records_task_metrics_and_attaches_pipeline(tmp_path):
    from forge.core.task_coordinator import TaskExecutionCoordinator
    from forge.core.task_queue import PersistentTaskQueue
    from forge.core.task_recovery import RecoveryPolicy, TaskRecoveryEngine
    from forge.core.task_store import TaskStore

    store = TaskStore(tmp_path / "tasks.db")
    queue = PersistentTaskQueue(store=store)
    queue.add("coord-1", "Fix the bug, implement the code, run tests.")

    recovery = TaskRecoveryEngine(
        store,
        RecoveryPolicy(max_attempts=2),
    )

    recorder = MetricsRecorder()
    coordinator = TaskExecutionCoordinator(
        queue,
        recovery,
        metrics=recorder,
        timer=FakeTimer(step=0.5),
    )

    from forge.agents.execution import CallableAgentExecutor
    from forge.agents.registry import AgentRegistration, AgentRegistry

    registry = AgentRegistry()

    for name, role, capability in (
        ("planner", "planning", "planning"),
        ("coder", "coding", "coding"),
        ("tester", "testing", "testing"),
        ("reviewer", "reviewing", "review"),
    ):
        registry.register(
            AgentRegistration(
                name=name,
                role=role,
                capabilities=(capability,),
                executor=CallableAgentExecutor(
                    name,
                    lambda request: f"{name} output",
                ),
            )
        )

    pipeline = AgentPipeline(
        agent_registry=registry,
    )

    result = coordinator.execute_pipeline("coord-1", pipeline)

    assert result.success

    task_records = [
        record
        for record in recorder.records
        if record.stage == "task"
    ]

    assert len(task_records) == 1
    assert task_records[0].task_id == "coord-1"
    assert task_records[0].agent == "pipeline"
    assert task_records[0].status == "success"
    assert task_records[0].duration_ms == 500.0

    stage_records = recorder.by_stage("coding")

    assert stage_records
    assert pipeline.metrics_recorder is recorder