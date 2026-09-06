from __future__ import annotations

from unittest.mock import MagicMock

from forge.agents.execution import AgentRequest, CallableAgentExecutor
from forge.agents.git import GitAgent, GitExecutor
from forge.agents.planner import CapabilityAgentPlanner
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.agents.requirements import TaskRequirementExtractor
from forge.agents.validator import AgentPlanValidator
from forge.core.agent_pipeline import AgentPipeline
from forge.core.task_engine import Task, TaskStatus
from forge.intelligence.repository import RepositoryIntelligence
from forge.tools.git import SafeGit


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


def test_git_agent_keeps_existing_interface(tmp_path):
    intelligence = build_project(tmp_path)
    agent = GitAgent()

    assert agent.name == "git"
    assert "git" in agent.describe().lower()

    context = agent.build_context(
        intelligence,
        "create a feature branch",
        target_files=("demo/models.py",),
    )

    assert "demo/models.py" in context.files


def test_git_executor_produces_plan():
    executor = GitExecutor()

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="git-1",
                description="create a feature branch and commit changes",
            ),
            stage=TaskStatus.RUNNING,
        )
    )

    assert response.success
    assert response.agent == "git"
    assert "# Git Automation Plan" in response.output
    assert "create_branch" in response.output
    assert "commit" in response.output


def test_git_executor_with_safe_git_shows_state():
    tool = MagicMock()
    tool.run = MagicMock(return_value=MagicMock(
        returncode=0,
        stdout="feature-x\n",
        stderr="",
    ))
    tool.status = MagicMock(return_value=" M file.py")
    safe_git = SafeGit(tool=tool)
    executor = GitExecutor(safe_git=safe_git)

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="git-2",
                description="push the current branch",
            ),
            stage=TaskStatus.RUNNING,
        )
    )

    assert response.success
    assert "feature-x" in response.output
    assert "push" in response.output


def test_git_executor_no_operations_detected():
    executor = GitExecutor()

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="git-3",
                description="analyze the code quality",
            ),
            stage=TaskStatus.RUNNING,
        )
    )

    assert response.success
    assert "no git operations" in response.output.lower()


def test_git_capability_is_extracted():
    requirements = TaskRequirementExtractor().extract(
        "create a new branch and commit the changes"
    )

    assert "git" in requirements.capabilities
    assert "git" in requirements.roles


def test_git_pr_keywords_extract():
    requirements = TaskRequirementExtractor().extract(
        "open a pull request for the new feature"
    )

    assert "git" in requirements.capabilities


def test_plan_with_git_executes_and_validates(tmp_path):
    registry = AgentRegistry()

    registry.register(
        AgentRegistration(
            name="git",
            role="git",
            capabilities=("git",),
            executor=CallableAgentExecutor(
                "git",
                lambda request: "git plan created",
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

    description = "implement the feature and create a pull request"

    plan = CapabilityAgentPlanner(registry).plan(description)

    assert "git" in plan.capabilities
    assert "git" in plan.names

    validation = AgentPlanValidator(registry).validate(plan)
    assert validation.valid
    assert validation.issues == ()

    pipeline = AgentPipeline(agent_registry=registry)

    task = Task(id="git-plan-1", description=description)
    results = pipeline.execute_plan(task, plan)

    assert "git" in [result.agent for result in results]
    assert all(result.success for result in results)
    assert task.status == TaskStatus.COMPLETED


def test_git_capability_maps_to_running_stage():
    assert (
        AgentPipeline.CAPABILITY_STAGES["git"]
        == TaskStatus.RUNNING
    )

    validator = AgentPlanValidator(AgentRegistry())
    assert validator._role_for_capability("git") == "git"
