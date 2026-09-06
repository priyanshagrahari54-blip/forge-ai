from forge.agents.execution import AgentRequest, CallableAgentExecutor
from forge.agents.researcher import ResearchAgent, ResearchExecutor
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.agents.requirements import TaskRequirementExtractor
from forge.agents.validator import AgentPlanValidator
from forge.core.agent_pipeline import AgentPipeline
from forge.core.task_engine import Task, TaskStatus
from forge.intelligence.repository import RepositoryIntelligence


def build_project(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "demo" / "__init__.py").write_text("")
    (tmp_path / "demo" / "models.py").write_text(
        "class User:\n"
        "    pass\n"
    )
    (tmp_path / "demo" / "service.py").write_text(
        "from demo.models import User\n\n"
        "def create_user():\n"
        "    return User()\n"
    )
    (tmp_path / "tests" / "test_service.py").write_text(
        "from demo.service import create_user\n\n"
        "def test_create_user():\n"
        "    create_user()\n"
    )

    return RepositoryIntelligence.build(tmp_path)


def test_research_agent_keeps_existing_interface(tmp_path):
    intelligence = build_project(tmp_path)
    agent = ResearchAgent()

    assert agent.name == "researcher"
    assert "researching" in agent.describe()

    context = agent.build_context(
        intelligence,
        "understand create_user",
        target_files=("demo/service.py",),
    )

    assert "demo/service.py" in context.files


def test_research_executor_produces_report_from_context(tmp_path):
    intelligence = build_project(tmp_path)

    context = ResearchAgent().build_context(
        intelligence,
        "understand create_user",
    )

    executor = ResearchExecutor(intelligence)

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="research-1",
                description="understand create_user",
            ),
            stage=TaskStatus.RESEARCHING,
            context=context,
        )
    )

    assert response.success
    assert response.agent == "researcher"
    assert "# Research Report" in response.output
    assert "understand create_user" in response.output
    assert "demo/service.py" in response.output


def test_research_executor_enriches_files_with_intelligence(tmp_path):
    intelligence = build_project(tmp_path)

    context = ResearchAgent().build_context(
        intelligence,
        "understand create_user",
        target_files=("demo/service.py",),
    )

    response = ResearchExecutor(intelligence).execute(
        AgentRequest(
            task=Task(
                id="research-2",
                description="understand create_user",
            ),
            stage=TaskStatus.RESEARCHING,
            context=context,
        )
    )

    assert response.success
    assert "symbols:" in response.output
    assert "tests:" in response.output
    assert "test_service.py" in response.output


def test_research_executor_fails_without_context_or_intelligence():
    executor = ResearchExecutor()

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="research-3",
                description="understand the service",
            ),
            stage=TaskStatus.RESEARCHING,
            context=None,
        )
    )

    assert not response.success
    assert "requires" in response.error


def test_research_capability_is_extracted():
    requirements = TaskRequirementExtractor().extract(
        "research the service internals to understand the flow"
    )

    assert requirements.capabilities == ("research",)
    assert requirements.roles == ("researching",)


def test_research_comes_before_coding_in_plan():
    requirements = TaskRequirementExtractor().extract(
        "research the codebase options, then implement the feature"
    )

    assert requirements.capabilities == ("research", "coding")
    assert requirements.roles == ("researching", "coding")


def test_plan_with_research_executes_and_validates(tmp_path):
    registry = AgentRegistry()

    registry.register(
        AgentRegistration(
            name="researcher",
            role="researching",
            capabilities=("research",),
            executor=CallableAgentExecutor(
                "researcher",
                lambda request: "research complete",
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

    description = (
        "research the codebase options, then implement the feature"
    )

    from forge.agents.planner import CapabilityAgentPlanner

    plan = CapabilityAgentPlanner(registry).plan(description)

    assert plan.capabilities == ("research", "coding")
    assert plan.names == ("researcher", "coder")

    validation = AgentPlanValidator(registry).validate(plan)
    assert validation.valid
    assert validation.issues == ()

    pipeline = AgentPipeline(agent_registry=registry)

    task = Task(id="research-plan-1", description=description)
    results = pipeline.execute_plan(task, plan)

    assert [result.agent for result in results] == [
        "researcher",
        "coder",
    ]
    assert [result.stage for result in results] == [
        TaskStatus.RESEARCHING,
        TaskStatus.CODING,
    ]
    assert all(result.success for result in results)
    assert task.status == TaskStatus.COMPLETED


def test_research_capability_maps_to_researching_stage():
    assert (
        AgentPipeline.CAPABILITY_STAGES["research"]
        == TaskStatus.RESEARCHING
    )

    validator = AgentPlanValidator(AgentRegistry())
    assert validator._role_for_capability("research") == "researching"