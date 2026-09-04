from __future__ import annotations

from pathlib import Path

from forge.core.agent_pipeline import AgentPipeline
from forge.core.pipeline_agents import ContextAwareStageAgent
from forge.core.task_engine import Task, TaskStatus
from forge.intelligence.repository import RepositoryIntelligence


def make_repo(tmp_path: Path) -> RepositoryIntelligence:
    package = tmp_path / "demo"
    package.mkdir()

    (package / "__init__.py").write_text("")
    (package / "service.py").write_text(
        "def build_feature():\n"
        "    return 'feature'\n"
    )

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_service.py").write_text(
        "from demo.service import build_feature\n\n"
        "def test_build_feature():\n"
        "    assert build_feature() == 'feature'\n"
    )

    return RepositoryIntelligence.build(tmp_path)


def make_task() -> Task:
    return Task(
        id="task-1",
        description="Implement build_feature",
    )


def test_context_aware_agent_builds_context(tmp_path: Path) -> None:
    intelligence = make_repo(tmp_path)
    received = {}

    def worker(task, context):
        received["context"] = context
        return "implemented"

    agent = ContextAwareStageAgent(
        "coder",
        TaskStatus.CODING,
        worker,
        intelligence,
    )

    result = agent.execute(make_task())

    assert result.success
    assert result.output == "implemented"
    assert result.context_fingerprint
    assert received["context"].fingerprint == result.context_fingerprint


def test_context_contains_repository_files(tmp_path: Path) -> None:
    intelligence = make_repo(tmp_path)
    received = {}

    def worker(task, context):
        received["context"] = context
        return "ok"

    agent = ContextAwareStageAgent(
        "coder",
        TaskStatus.CODING,
        worker,
        intelligence,
    )

    agent.execute(make_task())

    assert received["context"].files


def test_explicit_context_is_reused(tmp_path: Path) -> None:
    intelligence = make_repo(tmp_path)
    calls = []

    def worker(task, context):
        calls.append(context)
        return "ok"

    agent = ContextAwareStageAgent(
        "coder",
        TaskStatus.CODING,
        worker,
        intelligence,
    )

    context = agent.build_context(make_task())
    result = agent.execute(make_task(), context)

    assert calls == [context]
    assert result.context_fingerprint == context.fingerprint


def test_context_budget_is_respected(tmp_path: Path) -> None:
    intelligence = make_repo(tmp_path)

    agent = ContextAwareStageAgent(
        "coder",
        TaskStatus.CODING,
        lambda task, context: str(context.estimated_tokens),
        intelligence,
        max_tokens=100,
    )

    result = agent.execute(make_task())

    assert result.success
    assert int(result.output) <= 100


def test_pipeline_preserves_context_fingerprint(tmp_path: Path) -> None:
    intelligence = make_repo(tmp_path)

    agents = {
        TaskStatus.PLANNING: ContextAwareStageAgent(
            "planner",
            TaskStatus.PLANNING,
            lambda task, context: "plan",
            intelligence,
        ),
        TaskStatus.CODING: ContextAwareStageAgent(
            "coder",
            TaskStatus.CODING,
            lambda task, context: "code",
            intelligence,
        ),
        TaskStatus.TESTING: ContextAwareStageAgent(
            "tester",
            TaskStatus.TESTING,
            lambda task, context: "test",
            intelligence,
        ),
        TaskStatus.REVIEWING: ContextAwareStageAgent(
            "reviewer",
            TaskStatus.REVIEWING,
            lambda task, context: "review",
            intelligence,
        ),
    }

    pipeline = AgentPipeline(stage_agents=agents)
    results = pipeline.execute(make_task())

    assert len(results) == 4
    assert all(result.success for result in results)
    assert all(result.context_fingerprint for result in results)
