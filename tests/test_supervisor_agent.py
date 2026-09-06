from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from forge.agents.execution import AgentRequest, CallableAgentExecutor
from forge.agents.planner import CapabilityAgentPlanner
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.agents.requirements import TaskRequirementExtractor
from forge.agents.supervisor import SupervisorAgent, SupervisorExecutor
from forge.agents.validator import AgentPlanValidator
from forge.core.agent_pipeline import AgentPipeline
from forge.core.supervisor import Supervisor
from forge.core.task_engine import Task, TaskEngine, TaskStatus
from forge.intelligence.repository import RepositoryIntelligence


def build_project(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "demo" / "__init__.py").write_text("")
    (tmp_path / "demo" / "models.py").write_text(
        "class User:\n    pass\n"
    )
    (tmp_path / "tests" / "test_models.py").write_text(
        "from demo.models import User\n"
        "def test_user():\n    User()\n"
    )

    return RepositoryIntelligence.build(tmp_path)


def test_supervisor_agent_keeps_existing_interface(tmp_path):
    intelligence = build_project(tmp_path)
    agent = SupervisorAgent()

    assert agent.name == "supervisor"
    assert "orchestration" in agent.describe().lower()

    context = agent.build_context(
        intelligence,
        "orchestrate the workflow",
        target_files=("demo/models.py",),
    )

    assert "demo/models.py" in context.files


def test_supervisor_executor_produces_plan():
    executor = SupervisorExecutor()

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="sup-1",
                description="orchestrate multiple tasks with dependencies",
            ),
            stage=TaskStatus.RUNNING,
        )
    )

    assert response.success
    assert response.agent == "supervisor"
    assert "# Supervisor Orchestration Plan" in response.output
    assert "orchestrate" in response.output


def test_supervisor_executor_with_supervisor_shows_state():
    engine = TaskEngine()
    engine.add("task-1", "implement feature A")
    engine.add("task-2", "implement feature B", dependencies=["task-1"])
    supervisor = Supervisor("test", engine=engine)
    executor = SupervisorExecutor(supervisor=supervisor)

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="sup-2",
                description="run the workflow",
            ),
            stage=TaskStatus.RUNNING,
        )
    )

    assert response.success
    assert "ready tasks: 1" in response.output
    assert "blocked tasks: 1" in response.output
    assert "task-1" in response.output
    assert "task-2" in response.output


def test_supervisor_executor_detects_dependency_operations():
    executor = SupervisorExecutor()

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="sup-3",
                description="resolve dependencies and determine execution order",
            ),
            stage=TaskStatus.RUNNING,
        )
    )

    assert response.success
    assert "resolve_dependencies" in response.output


def test_supervisor_executor_detects_retry_operations():
    executor = SupervisorExecutor()

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="sup-4",
                description="retry failed tasks with resilient failure handling",
            ),
            stage=TaskStatus.RUNNING,
        )
    )

    assert response.success
    assert "retry" in response.output


def test_supervisor_executor_detects_parallel_operations():
    executor = SupervisorExecutor()

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="sup-5",
                description="execute tasks in parallel and concurrently",
            ),
            stage=TaskStatus.RUNNING,
        )
    )

    assert response.success
    assert "parallel" in response.output


def test_supervisor_executor_no_operations_detected():
    executor = SupervisorExecutor()

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="sup-6",
                description="analyze the code quality",
            ),
            stage=TaskStatus.RUNNING,
        )
    )

    assert response.success
    assert "no orchestration operations" in response.output.lower()


def test_supervisor_capability_is_extracted():
    requirements = TaskRequirementExtractor().extract(
        "orchestrate the multi-task workflow"
    )

    assert "supervisor" in requirements.capabilities
    assert "supervisor" in requirements.roles


def test_supervisor_keywords_extract():
    requirements = TaskRequirementExtractor().extract(
        "coordinate the tasks and manage the workflow"
    )

    assert "supervisor" in requirements.capabilities


def test_plan_with_supervisor_executes_and_validates(tmp_path):
    registry = AgentRegistry()

    registry.register(
        AgentRegistration(
            name="supervisor",
            role="supervisor",
            capabilities=("supervisor",),
            executor=CallableAgentExecutor(
                "supervisor",
                lambda request: "supervisor plan created",
            ),
        )
    )

    registry.register(
        AgentRegistration(
            name="coder",
            role="coding",
            capabilities=("coding",),
            executor=CallableAgentExecutor(
                "coder",
                lambda request: "coded",
            ),
        )
    )

    description = "implement the feature and orchestrate the workflow"

    plan = CapabilityAgentPlanner(registry).plan(description)

    assert "supervisor" in plan.capabilities
    assert "supervisor" in plan.names

    validation = AgentPlanValidator(registry).validate(plan)
    assert validation.valid
    assert validation.issues == ()

    pipeline = AgentPipeline(agent_registry=registry)

    task = Task(id="sup-plan-1", description=description)
    results = pipeline.execute_plan(task, plan)

    assert "supervisor" in [result.agent for result in results]
    assert all(result.success for result in results)
    assert task.status == TaskStatus.COMPLETED


def test_supervisor_capability_maps_to_running_stage():
    assert (
        AgentPipeline.CAPABILITY_STAGES["supervisor"]
        == TaskStatus.RUNNING
    )

    validator = AgentPlanValidator(AgentRegistry())
    assert validator._role_for_capability("supervisor") == "supervisor"


class TestSupervisorCore:
    """Tests for the core Supervisor orchestration logic."""

    def test_supervisor_adds_tasks(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine)

        supervisor.add_task("task-1", "first task")
        supervisor.add_task("task-2", "second task", dependencies=["task-1"])

        assert len(supervisor.get_ready_tasks()) == 1
        assert len(supervisor.get_blocked_tasks()) == 1

    def test_supervisor_respects_dependencies(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine)

        supervisor.add_task("task-1", "first task")
        supervisor.add_task("task-2", "second task", dependencies=["task-1"])

        ready = supervisor.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "task-1"

    def test_supervisor_run_completes_all_tasks(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine)

        supervisor.add_task("task-1", "first task")
        supervisor.add_task("task-2", "second task", dependencies=["task-1"])

        def executor(task):
            return True

        stats = supervisor.run(executor)

        assert stats.completed == 2
        assert stats.failed == 0

    def test_supervisor_run_handles_failures(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine, max_retries=1)

        supervisor.add_task("task-1", "failing task")

        def executor(task):
            return False

        stats = supervisor.run(executor)

        assert stats.failed == 1
        assert stats.completed == 0

    def test_supervisor_decide_skips_completed(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine)

        task = engine.add("task-1", "done task")
        engine.complete("task-1")

        decision = supervisor.decide(task)
        assert decision.action == "skip"

    def test_supervisor_decide_retries_failed(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine, max_retries=3)

        task = engine.add("task-1", "failing task")
        engine.fail(task.id, "error")

        decision = supervisor.decide(task)
        assert decision.action == "retry"

    def test_supervisor_decide_waits_for_dependencies(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine)

        engine.add("task-1", "first")
        task2 = engine.add("task-2", "second", dependencies=["task-1"])

        decision = supervisor.decide(task2)
        assert decision.action == "wait"

    def test_supervisor_decide_executes_ready(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine)

        task = engine.add("task-1", "ready task")

        decision = supervisor.decide(task)
        assert decision.action == "execute"

    def test_supervisor_max_cycles_raises(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine)

        with pytest.raises(ValueError, match="max_cycles"):
            supervisor.run(lambda t: True, max_cycles=0)

    def test_supervisor_reset(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine)

        supervisor.add_task("task-1", "task")
        engine.complete("task-1")

        supervisor.reset()

        assert all(t.status == TaskStatus.PENDING for t in engine.tasks)

    def test_supervisor_detects_deadlock(self):
        engine = TaskEngine()
        supervisor = Supervisor("test", engine=engine)

        # Build a circular dependency using valid engine operations:
        # add tasks first, then connect dependencies both directions.
        engine.add("task-1", "first")
        engine.add("task-2", "second")
        engine.add_dependency("task-1", "task-2")
        engine.add_dependency("task-2", "task-1")

        def executor(task):
            return True

        stats = supervisor.run(executor)

        # Both should fail since neither can ever be ready
        assert stats.failed == 2