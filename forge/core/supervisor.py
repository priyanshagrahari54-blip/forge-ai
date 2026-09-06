from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.agents.coder import CoderAgent
from forge.agents.debugger import DebuggerAgent, TestDebugLoop
from forge.agents.planner import AgentPlan, CapabilityAgentPlanner
from forge.agents.registry import AgentRegistry, default_registry
from forge.agents.validator import AgentPlanValidator
from forge.core.planner import Planner
from forge.core.state import ForgeState
from forge.core.task_engine import Task
from forge.models.router import ModelInfo, ModelRouter
from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import PermissionManager
from forge.security.verification import VerificationPipeline, VerificationResult
from forge.tools.checkpoint import CheckpointManager


@dataclass
class SupervisorRunResult:
    success: bool
    task_id: str
    requirement: str
    plan: AgentPlan | None
    selected_model: str = ""
    verification_results: list[VerificationResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    final_state: str = "FAILED"
    metadata: dict[str, Any] = field(default_factory=dict)


class Supervisor:
    """Supervises end-to-end engineering tasks through planning, routing, tool runtime, debug loop, verification, and checkpoints."""

    def __init__(
        self,
        project_name: str = "forge_project",
        root: str = ".",
        registry: AgentRegistry | None = None,
        model_router: ModelRouter | None = None,
    ) -> None:
        self.root = str(Path(root).resolve())
        self.state = ForgeState(project_name)
        self.planner = Planner()
        self.registry = registry or default_registry()

        if model_router is None:
            self.model_router = ModelRouter(prefer_local=True)
            self.model_router.register(
                ModelInfo(
                    name="mock-model",
                    capability="coding",
                    available=True,
                    is_local=True,
                )
            )
            self.model_router.register(
                ModelInfo(
                    name="mock-model",
                    capability="testing",
                    available=True,
                    is_local=True,
                )
            )
            self.model_router.register(
                ModelInfo(
                    name="mock-model",
                    capability="debugging",
                    available=True,
                    is_local=True,
                )
            )
            self.model_router.register(
                ModelInfo(
                    name="mock-model",
                    capability="review",
                    available=True,
                    is_local=True,
                )
            )
        else:
            self.model_router = model_router

        self.permission_manager = PermissionManager()
        self.runtime = create_default_runtime(self.permission_manager, root=self.root)
        self.checkpoint_manager = CheckpointManager(root=self.root)
        self.verification_pipeline = VerificationPipeline()
        self.debugger = DebuggerAgent()
        self.debug_loop = TestDebugLoop(debugger=self.debugger, max_retries=3)

    def create_plan(self, request: str) -> AgentPlan:
        planner = CapabilityAgentPlanner(self.registry)
        return planner.plan(request)

    def start(self, task: str) -> None:
        self.state.start_task(task)

    def complete(self) -> None:
        self.state.complete_task()

    def run_task(
        self,
        requirement: str,
        changes: dict[str, str] | None = None,
        test_command: list[str] | None = None,
        approved: bool = True,
    ) -> SupervisorRunResult:
        self.start(requirement)
        task = Task(id="task-sup-1", description=requirement)

        # 1. Planning
        try:
            plan = self.create_plan(requirement)
        except Exception as exc:
            self.state.fail(f"Planning failed: {exc}")
            return SupervisorRunResult(
                success=False,
                task_id=task.id,
                requirement=requirement,
                plan=None,
                errors=[f"Planning failed: {exc}"],
            )

        # 2. Plan Validation Gate
        validator = AgentPlanValidator(self.registry)
        if not plan.is_empty():
            val_res = validator.validate(plan)
            if not val_res.valid:
                err_msg = f"Plan validation failed: {'; '.join(val_res.messages)}"
                self.state.fail(err_msg)
                return SupervisorRunResult(
                    success=False,
                    task_id=task.id,
                    requirement=requirement,
                    plan=plan,
                    errors=[err_msg],
                )

        # 3. Model Selection / Routing
        route_decision = self.model_router.route("coding")
        selected_model = (
            route_decision.selected_model.name
            if route_decision.selected_model
            else "default"
        )

        # 4. Checkpoint creation
        target_files = list(changes.keys()) if changes else []
        chk_id = self.checkpoint_manager.create_checkpoint(
            target_files=target_files,
            description=f"Before executing requirement: {requirement}",
        )

        # 5. Execute Code Modification
        coder = CoderAgent(runtime=self.runtime, root=self.root)
        if changes:
            for path, content in changes.items():
                write_res = coder.write_file(path, content, approved=approved)
                if not write_res.success:
                    self.checkpoint_manager.rollback(chk_id)
                    self.state.fail(
                        f"Code modification blocked/failed: {write_res.error}"
                    )
                    return SupervisorRunResult(
                        success=False,
                        task_id=task.id,
                        requirement=requirement,
                        plan=plan,
                        selected_model=selected_model,
                        errors=[f"Write failed: {write_res.error}"],
                    )

        # 6. Test & Debug Retry Loop
        cmd = test_command or ["pytest", "-q"]

        def test_runner():
            return coder.run_tests(command=cmd, approved=approved)

        debug_res = self.debug_loop.run(task, test_runner=test_runner)

        if not debug_res.success:
            self.checkpoint_manager.rollback(chk_id)
            self.state.fail(f"Test/Debug loop failed: {debug_res.error}")
            return SupervisorRunResult(
                success=False,
                task_id=task.id,
                requirement=requirement,
                plan=plan,
                selected_model=selected_model,
                errors=[f"Tests failed: {debug_res.error}"],
            )

        # 7. Verification Gates (Review & Security)
        verif_results = self.verification_pipeline.verify_candidate(
            changes=changes or {},
            test_result=test_runner(),
        )

        verif_passed = all(vr.success for vr in verif_results)
        if not verif_passed:
            self.checkpoint_manager.rollback(chk_id)
            verif_errs = [e for vr in verif_results for e in vr.errors]
            err_msg = f"Verification failed: {'; '.join(verif_errs)}"
            self.state.fail(err_msg)
            return SupervisorRunResult(
                success=False,
                task_id=task.id,
                requirement=requirement,
                plan=plan,
                selected_model=selected_model,
                verification_results=verif_results,
                errors=[err_msg],
            )

        # 8. Accept / Commit Checkpoint
        self.checkpoint_manager.commit_checkpoint(
            chk_id, message=f"Accepted changes for: {requirement}"
        )
        self.complete()

        return SupervisorRunResult(
            success=True,
            task_id=task.id,
            requirement=requirement,
            plan=plan,
            selected_model=selected_model,
            verification_results=verif_results,
            final_state="COMPLETED",
        )
