from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from forge.core.planner import Planner
from forge.core.state import ForgeState


class Supervisor:
    """Coordinates one complete, guarded software-engineering transaction."""

    def __init__(self, project_name: str, root: str | Path = ".") -> None:
        self.state = ForgeState(project_name)
        self.planner = Planner()
        self.root = Path(root).resolve()
        self.current_stage = "IDLE"
        self.stage_history: list[dict[str, Any]] = []

    def create_plan(self, request: str):
        return self.planner.create_plan(request)

    def start(self, task: str) -> None:
        self.state.start_task(task)

    def complete(self) -> None:
        self.state.complete_task()

    def set_stage(self, stage: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.current_stage = stage
        self.stage_history.append({"stage": stage, "details": details or {}})

    def execute_self_development_stage(self, stage: str, stage_fn: Callable[[], Any], details: Optional[Dict[str, Any]] = None) -> Any:
        self.set_stage(stage, details)
        return stage_fn()

    def run(
        self,
        requirement: str,
        *,
        approved: bool = False,
        router=None,
        max_debug_retries: int = 3,
    ) -> dict[str, Any]:
        """Execute model → code → test/debug → review/security → acceptance.

        The only model-controlled artifact is the structured response returned by
        the provider. This method deliberately has no ``changes`` or modifier
        argument. Every write is checkpointed and every commit uses the exact
        files reported by the coding agent.
        """
        from forge.agents.coder import CoderAgent
        from forge.agents.debugger import DebuggerAgent, TestDebugLoop
        from forge.agents.execution import AgentRequest, CallableAgentExecutor
        from forge.agents.registry import AgentRegistration, AgentRegistry
        from forge.agents.planner import CapabilityAgentPlanner
        from forge.core.task_engine import TaskEngine, TaskStatus
        from forge.intelligence.repository import RepositoryIntelligence
        from forge.models.router import ModelInfo, ModelRouter
        from forge.models.provider import LocalModelProvider
        from forge.security.verification import VerificationPipeline
        from forge.self_development.benchmark import BenchmarkRunner
        from forge.tools.checkpoint import CheckpointManager
        from forge.tools.git import GitTool

        if not approved:
            return {"accepted": False, "stage": "APPROVAL_REQUIRED", "error": "Explicit write approval is required"}

        # A supplied router is an adapter for a real provider, not a change set.
        if router is None:
            router = ModelRouter([ModelInfo("local", "coding", available=True, free=True, provider=LocalModelProvider(), capabilities=("coding", "debugging"))])
        git = GitTool(self.root)
        checkpoint_manager = CheckpointManager(self.root)
        checkpoint = checkpoint_manager.create("supervisor-run")
        engine = TaskEngine()
        task = engine.add("supervisor-task", requirement)
        touched: list[str] = []
        result: dict[str, Any] = {"accepted": False, "stages": [], "attempts": [], "gates": []}

        def stage(name: str) -> None:
            self.set_stage(name)
            result["stages"].append(name)

        try:
            stage("PLAN")
            intelligence = RepositoryIntelligence.build(self.root)
            task.status = TaskStatus.PLANNING
            coder = CoderAgent(root=str(self.root), router=router)
            debugger = DebuggerAgent(str(self.root), router=router)
            registry = AgentRegistry([
                AgentRegistration("coder", "coding", coder, ("coding",)),
                AgentRegistration("debugger", "debugging", debugger, ("debugging",)),
                AgentRegistration("reviewer", "reviewing", CallableAgentExecutor("reviewer", lambda request: "independent review"), ("review",)),
                AgentRegistration("tester", "testing", CallableAgentExecutor("tester", lambda request: "test execution is performed by TestDebugLoop"), ("testing",)),
                AgentRegistration("security", "security", CallableAgentExecutor("security", lambda request: "security verification is performed by VerificationPipeline"), ("security",)),
            ])
            agent_plan = CapabilityAgentPlanner(registry).plan(requirement)
            result["plan"] = {"agents": list(agent_plan.names), "capabilities": list(agent_plan.capabilities)}
            if not agent_plan.agents or "coder" not in agent_plan.names:
                raise RuntimeError("capability planner could not select a coding agent")
            stage("AGENTS")
            stage("MODEL")
            task.status = TaskStatus.CODING
            context = coder.build_context(intelligence, requirement)
            response = coder.execute(AgentRequest(task, TaskStatus.CODING, context=context, instructions=requirement, metadata={"approved": True}))
            if not response.success:
                raise RuntimeError(response.error or "model coding failed")
            touched = list(response.metadata.get("files", []))

            stage("CODE")
            stage("TEST")
            loop = TestDebugLoop(self.root, max_retries=max_debug_retries, debugger=debugger)
            debug_result = loop.run(requirement, context=str(context), approved=True)
            result["attempts"] = [asdict(attempt) for attempt in debug_result.attempts]
            if not debug_result.success:
                raise RuntimeError(debug_result.error or "tests did not pass after bounded repairs")
            # A repair is still model output, so include newly touched files in
            # the eventual explicit staging set.
            repaired_files = [path for attempt in result["attempts"] for path in attempt["modifications"]]
            touched = sorted(set(touched) | set(repaired_files))

            if result["attempts"]:
                stage("DEBUG")
                stage("REPAIR")
                stage("RETEST")
            else:
                stage("RETEST")
            stage("REVIEW")
            verification = VerificationPipeline(self.root)
            diff = git.diff() + "\n" + git.status()
            review = verification.review(diff)
            stage("SECURITY")
            security = verification.security()
            stage("BENCHMARK")
            benchmark = BenchmarkRunner(self.root).run_benchmarks()
            stage("ACCEPTANCE")
            gates = [verification.tests(), verification.build(), verification.lint(), security, review]
            result["gates"] = [asdict(gate) for gate in gates]
            result["benchmark"] = benchmark.to_dict()
            if not all(gate.passed for gate in gates) or benchmark.passed_benchmarks < benchmark.total_benchmarks:
                raise RuntimeError("verification or benchmark gate failed")

            stage("CHECKPOINT")
            # stage_files is deliberately explicit and rejects Forge state.
            touched = [path for path in sorted(set(touched)) if not path.startswith(".forge/")]
            if not touched:
                raise RuntimeError("model produced no accepted files")
            stage("COMMIT")
            commit = git.commit_files(touched, "forge: " + requirement)
            if commit.returncode != 0:
                raise RuntimeError(commit.stderr.strip() or "git commit failed")
            checkpoint_manager.cleanup(checkpoint)
            result.update(accepted=True, files=touched, model=response.metadata.get("model"))
            stage("COMPLETED")
            return result
        except Exception as exc:
            stage("ROLLBACK")
            # Restore only files belonging to this run. Unrelated user files are
            # not deleted or rewritten.
            checkpoint_manager.rollback(checkpoint, sorted(set(touched)))
            result["error"] = str(exc)
            return result
