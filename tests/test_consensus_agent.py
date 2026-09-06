from __future__ import annotations

from forge.agents.consensus import ConsensusAgent, ConsensusExecutor
from forge.agents.execution import AgentRequest, CallableAgentExecutor
from forge.agents.planner import CapabilityAgentPlanner
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.agents.requirements import TaskRequirementExtractor
from forge.agents.validator import AgentPlanValidator
from forge.consensus.engine import ConsensusEngine
from forge.core.agent_pipeline import AgentPipeline
from forge.core.task_engine import Task, TaskStatus


def test_consensus_agent_interface():
    agent = ConsensusAgent()

    assert agent.name == "consensus"
    assert "consensus" in agent.describe().lower()


def test_consensus_executor_usage_report():
    executor = ConsensusExecutor()

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="consensus-1",
                description="determine consensus",
            ),
            stage=TaskStatus.REVIEWING,
        )
    )

    assert response.success
    assert response.agent == "consensus"
    assert "# Consensus Report" in response.output
    assert "Usage" in response.output
    assert "majority" in response.output


def test_consensus_executor_evaluates_responses():
    engine = ConsensusEngine(strategy="majority", quorum=0.5)
    executor = ConsensusExecutor(engine=engine)

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="consensus-2",
                description="vote on the options",
            ),
            stage=TaskStatus.REVIEWING,
            metadata={
                "responses": [
                    {"agent": "a", "output": "option-1", "success": True},
                    {"agent": "b", "output": "option-1", "success": True},
                    {"agent": "c", "output": "option-2", "success": True},
                ],
            },
        )
    )

    assert response.success
    assert "Consensus reached: True" in response.output
    assert "option-1" in response.output


def test_consensus_executor_detects_tie():
    engine = ConsensusEngine(strategy="majority", quorum=0.5)
    executor = ConsensusExecutor(engine=engine)

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="consensus-3",
                description="vote on the options",
            ),
            stage=TaskStatus.REVIEWING,
            metadata={
                "responses": [
                    {"agent": "a", "output": "option-1", "success": True},
                    {"agent": "b", "output": "option-2", "success": True},
                ],
            },
        )
    )

    assert response.success
    assert "Consensus reached: False" in response.output
    assert "Tie: True" in response.output


def test_consensus_executor_unanimous_strategy():
    engine = ConsensusEngine(strategy="unanimous")
    executor = ConsensusExecutor(engine=engine)

    response = executor.execute(
        AgentRequest(
            task=Task(
                id="consensus-4",
                description="unanimous vote",
            ),
            stage=TaskStatus.REVIEWING,
            metadata={
                "responses": [
                    {"agent": "a", "output": "option-1", "success": True},
                    {"agent": "b", "output": "option-1", "success": True},
                ],
            },
        )
    )

    assert response.success
    assert "Consensus reached: True" in response.output


def test_consensus_capability_is_extracted():
    requirements = TaskRequirementExtractor().extract(
        "reach consensus on the best approach"
    )

    assert "consensus" in requirements.capabilities
    assert "consensus" in requirements.roles


def test_consensus_vote_keywords_extract():
    requirements = TaskRequirementExtractor().extract(
        "have multiple reviewers vote on the implementation"
    )

    assert "consensus" in requirements.capabilities


def test_plan_with_consensus_executes_and_validates():
    registry = AgentRegistry()

    registry.register(
        AgentRegistration(
            name="consensus",
            role="consensus",
            capabilities=("consensus",),
            executor=CallableAgentExecutor(
                "consensus",
                lambda request: "consensus report",
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

    description = "implement the feature and reach consensus on the approach"

    plan = CapabilityAgentPlanner(registry).plan(description)

    assert "consensus" in plan.capabilities
    assert "consensus" in plan.names

    validation = AgentPlanValidator(registry).validate(plan)
    assert validation.valid
    assert validation.issues == ()

    pipeline = AgentPipeline(agent_registry=registry)

    task = Task(id="consensus-plan-1", description=description)
    results = pipeline.execute_plan(task, plan)

    assert "consensus" in [result.agent for result in results]
    assert all(result.success for result in results)
    assert task.status == TaskStatus.COMPLETED


def test_consensus_capability_maps_to_reviewing_stage():
    assert (
        AgentPipeline.CAPABILITY_STAGES["consensus"]
        == TaskStatus.REVIEWING
    )

    validator = AgentPlanValidator(AgentRegistry())
    assert validator._role_for_capability("consensus") == "consensus"
