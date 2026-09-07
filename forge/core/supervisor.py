from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Dict, Optional
from uuid import uuid4
from forge.core.planner import Planner
from forge.core.state import ForgeState
from forge.security.permissions import OperationMode


def _history_latency_seconds(history) -> float:
    """Sum routing-history latency in seconds.

    Fabric router history records milliseconds (and carries token counts);
    the legacy router records seconds. Both are measured values; this only
    normalizes the unit.
    """
    total = 0.0
    for entry in history or []:
        value = entry.get("latency") or 0.0
        total += value / 1000.0 if "tokens" in entry else value
    return total


def _history_token_usage(history) -> dict[str, int | None]:
    """Sum reported token counts, or ``None`` when no provider reported any."""
    inputs = [entry["tokens"].get("input") for entry in history or []
              if isinstance(entry.get("tokens"), dict)]
    outputs = [entry["tokens"].get("output") for entry in history or []
               if isinstance(entry.get("tokens"), dict)]
    if not inputs and not outputs:
        return {"input": None, "output": None}
    return {"input": sum(value or 0 for value in inputs),
            "output": sum(value or 0 for value in outputs)}


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
        fabric=None,
        max_debug_retries: int = 3,
        mode: OperationMode = OperationMode.ASSISTED,
    ) -> dict[str, Any]:
        """Execute model → code → test/debug → review/security → acceptance.

        Read-only work — repository inspection, planning, agent and model
        selection, proposal generation, test execution, verification — never
        requires write approval. Approval is evaluated at the actual
        permission boundary instead: every proposed write passes the
        PolicyGate, and nothing is written, committed, or staged unless the
        gate authorizes it under the active mode. ``approved`` carries the
        caller's explicit write approval; it satisfies REQUIRE_APPROVAL but
        can never override DENY.

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
        from forge.agents.reviewer import ReviewerAgent
        from forge.core.acceptance import AcceptanceEngine, GateOutcome
        from forge.core.report import TaskReport
        from forge.core.task_engine import TaskEngine, TaskStatus
        from forge.intelligence.repository import RepositoryIntelligence
        from forge.models.router import ModelInfo, ModelRouter
        from forge.models.provider import LocalModelProvider
        from forge.runtime.defaults import create_default_runtime
        from forge.security.permissions import PermissionManager
        from forge.security.policy_gate import PolicyGate
        from forge.security.review import ReviewGate
        from forge.security.verification import VerificationPipeline
        from forge.self_development.benchmark import BenchmarkRunner
        from forge.tools.checkpoint import CheckpointManager
        from forge.tools.git import GitTool

        # A supplied router (or fabric) is an adapter for a real provider, not
        # a change set. A caller may pass either the legacy router or the
        # centralized Model Fabric; the fabric is used when provided.
        if router is None and fabric is None:
            router = ModelRouter([ModelInfo("local", "coding", available=True, free=True, provider=LocalModelProvider(), capabilities=("coding", "debugging"))])
        mode = OperationMode(mode)
        permissions = PermissionManager(mode=mode)
        policy = PolicyGate(permissions)
        shared_runtime = create_default_runtime(permissions, str(self.root))
        started = perf_counter()
        run_id = uuid4().hex
        git = GitTool(self.root)
        checkpoint_manager = CheckpointManager(self.root)
        checkpoint = checkpoint_manager.create(f"supervisor-run-{run_id}")
        engine = TaskEngine()
        task = engine.add(f"supervisor-task-{run_id}", requirement)
        touched: list[str] = []
        files_read: list[str] = []
        commands_run: list[list[str]] = []
        result: dict[str, Any] = {
            "run_id": run_id,
            "requirement": requirement,
            "mode": mode.value,
            "task": {"id": task.id, "description": task.description},
            "accepted": False,
            "stages": [],
            "attempts": [],
            "gates": [],
            "rollback": False,
            "checkpoint_id": checkpoint.id,
        }

        def stage(name: str) -> None:
            self.set_stage(name)
            result["stages"].append(name)

        report = TaskReport(
            task_id=task.id,
            trace_id=run_id,
            requirement=requirement,
            checkpoint_id=checkpoint.id,
            mode=mode.value,
        )
        timings: dict[str, float] = {}

        def event(name: str, details: dict[str, Any] | None = None) -> None:
            report.record_event(name, perf_counter() - started, details or {})

        def timed(phase: str, phase_started: float) -> None:
            timings[phase] = perf_counter() - phase_started

        try:
            stage("PLAN")
            plan_started = perf_counter()
            event("task_started", {"requirement_chars": len(requirement)})
            intelligence = RepositoryIntelligence.build(self.root)
            task.status = TaskStatus.PLANNING
            coder = CoderAgent(runtime=shared_runtime, root=str(self.root), router=router, fabric=fabric)
            debugger = DebuggerAgent(str(self.root), runtime=shared_runtime, router=router, fabric=fabric)
            registry = AgentRegistry([
                AgentRegistration("coder", "coding", coder, ("coding",)),
                AgentRegistration("debugger", "debugging", debugger, ("debugging",)),
                AgentRegistration("reviewer", "reviewing", CallableAgentExecutor("reviewer", lambda request: "independent review"), ("review",)),
                AgentRegistration("tester", "testing", CallableAgentExecutor("tester", lambda request: "test execution is performed by TestDebugLoop"), ("testing",)),
                AgentRegistration("security", "security", CallableAgentExecutor("security", lambda request: "security verification is performed by VerificationPipeline"), ("security",)),
            ])
            planning_request = requirement if any(word in requirement.lower() for word in ("code", "implement", "add", "fix", "feature", "refactor")) else requirement + " implement code"
            agent_plan = CapabilityAgentPlanner(registry).plan(planning_request)
            result["plan"] = {"agents": list(agent_plan.names), "capabilities": list(agent_plan.capabilities)}
            result["selected_agents"] = list(agent_plan.names)
            if not agent_plan.agents or "coder" not in agent_plan.names:
                raise RuntimeError("capability planner could not select a coding agent")
            timed("plan", plan_started)
            event("agents_selected", {"agents": list(agent_plan.names)})
            stage("AGENTS")
            stage("MODEL")
            code_started = perf_counter()
            task.status = TaskStatus.CODING
            context = coder.build_context(intelligence, requirement)
            files_read = sorted({item.path for item in context.items})
            result["context_fingerprint"] = context.fingerprint
            response = coder.execute(AgentRequest(task, TaskStatus.CODING, context=context, instructions=requirement, metadata={"approved": approved}))
            touched = list(response.metadata.get("files", []))
            timed("code", code_started)
            if not response.success:
                for decision in coder.last_decisions:
                    event("permission_decision", decision)
                raise RuntimeError(response.error or "model coding failed")
            result["selected_model"] = response.metadata.get("model", "")
            result["selected_provider"] = response.metadata.get("provider", "")
            result["summary"] = response.metadata.get("summary", "")
            result["reasoning_summary"] = response.metadata.get("reasoning_summary", "")
            result["risks"] = response.metadata.get("risks", [])
            result["risk_level"] = response.metadata.get("risk_level", "NONE")
            if fabric is not None:
                result["model_routing"] = {
                    "policy": fabric.policy.to_dict(),
                    "history": list(fabric.router.history[-20:]),
                }
            event("model_selected", {"model": response.metadata.get("model", ""),
                                     "provider": response.metadata.get("provider", "")})
            event("change_proposed", {"files": list(touched),
                                      "summary_chars": len(result.get("summary", ""))})
            for decision in coder.last_decisions:
                event("permission_decision", decision)
            event("change_applied", {"files": list(touched)})

            stage("CODE")
            stage("TEST")
            test_started = perf_counter()
            loop = TestDebugLoop(self.root, max_retries=max_debug_retries, debugger=debugger)
            targeted = response.metadata.get("tests_to_run") or None
            debug_result = loop.run(requirement, context=str(context), approved=approved,
                                    test_paths=targeted)
            commands_run.append(list(loop.command))
            result["attempts"] = [asdict(attempt) for attempt in debug_result.attempts]
            result["retry_count"] = len(debug_result.attempts)
            result["test_result"] = {"passed": debug_result.success, "final_state": debug_result.final_state, "error": debug_result.error}
            timed("test", test_started)
            for attempt in debug_result.attempts:
                event("test_executed", {"attempt": attempt.attempt_number,
                                        "passed": attempt.test_passed,
                                        "exit_code": attempt.exit_code,
                                        "output_chars": len(attempt.test_output or "")})
                if not attempt.test_passed:
                    event("test_failed", {"attempt": attempt.attempt_number,
                                          "exit_code": attempt.exit_code,
                                          "reason": attempt.reason})
                if attempt.modifications:
                    event("repair_attempted", {"attempt": attempt.attempt_number,
                                               "files": sorted(attempt.modifications),
                                               "model": attempt.model})
            for decision in debugger.repair_decisions:
                event("permission_decision", decision)
            if not debug_result.attempts and debug_result.success:
                event("test_executed", {"runs": 1, "scope": debug_result.scope,
                                        "passed": True})
            # Attempted debugging is staged even when the loop ultimately
            # fails, so the stage history truthfully shows repair work.
            if result["attempts"]:
                stage("DEBUG")
                stage("REPAIR")
                stage("RETEST")
            else:
                stage("RETEST")
            if not debug_result.success:
                raise RuntimeError(debug_result.error or "tests did not pass after bounded repairs")
            # A repair is still model output, so include newly touched files in
            # the eventual explicit staging set.
            repaired_files = [path for attempt in result["attempts"] for path in attempt["modifications"]]
            touched = sorted(set(touched) | set(repaired_files))
            stage("REVIEW")
            review_started = perf_counter()
            verification = VerificationPipeline(self.root)
            diff = git.diff() + "\n" + git.status()
            repaired_files = [path for attempt in result["attempts"] for path in attempt["modifications"]]
            touched = sorted(set(touched) | set(repaired_files))
            review = verification.review(diff, touched)
            # Structured, severity-typed review (A32.8); a model-driven reviewer
            # contributes findings through the fabric when one is available.
            model_findings = None
            if fabric is not None:
                try:
                    model_findings = ReviewerAgent(fabric=fabric).review(requirement, diff, tuple(touched))
                except Exception:
                    model_findings = None
            review_decision = ReviewGate(self.root).review(
                diff, touched, requirement=requirement, model_findings=model_findings,
            )
            result["review"] = review_decision.to_dict()
            timed("review", review_started)
            event("review_result", {"verdict": review_decision.verdict.value,
                                    "findings": len(review_decision.findings)})
            stage("SECURITY")
            security_started = perf_counter()
            security = verification.security(touched)
            timed("security", security_started)
            event("security_result", {"passed": security.passed,
                                      "findings": len((security.evidence or {}).get("findings", []))})
            stage("BENCHMARK")
            benchmark_started = perf_counter()
            history_source = fabric.router.history if fabric is not None else router.history
            model_latency = _history_latency_seconds(history_source)
            token_usage = _history_token_usage(history_source)
            benchmark = BenchmarkRunner(self.root).run_benchmarks(
                task_success=debug_result.success,
                repair_attempts=len(result["attempts"]),
                files_changed=touched,
                model_latency=model_latency,
            )
            timed("benchmark", benchmark_started)
            event("benchmark_result", {"passed": benchmark.passed_benchmarks,
                                       "total": benchmark.total_benchmarks})
            stage("ACCEPTANCE")
            acceptance_started = perf_counter()
            gate_tests = verification.tests()
            gate_build = verification.build()
            gate_lint = verification.lint()
            gates = [gate_tests, gate_build, gate_lint, security, review]
            result["gates"] = [asdict(gate) for gate in gates]
            result["benchmark"] = benchmark.to_dict()
            benchmark_passed = benchmark.passed_benchmarks >= benchmark.total_benchmarks
            decision = AcceptanceEngine().decide(
                tests=gate_tests,
                build=gate_build,
                lint=gate_lint,
                review=review_decision,
                security=security,
                benchmark=GateOutcome("benchmark", benchmark_passed, "" if benchmark_passed else "benchmark incomplete"),
                permissions_ok=True,
                rollback_available=True,
                changed_files=touched,
            )
            result["acceptance"] = decision.to_dict()
            timed("acceptance", acceptance_started)
            event("acceptance_result", {"accepted": decision.accepted,
                                        "failed_gates": list(decision.failed_gates)})
            if not decision.accepted:
                raise RuntimeError("acceptance gate failed: " + "; ".join(decision.reasons))

            stage("CHECKPOINT")
            # stage_files is deliberately explicit and rejects Forge state.
            touched = [path for path in sorted(set(touched)) if not path.startswith(".forge/")]
            if not touched:
                raise RuntimeError("model produced no accepted files")
            # The commit itself is a permission boundary: even an accepted
            # candidate cannot commit unless the gate authorizes git_commit.
            commit_decision = policy.evaluate(
                operation="git_commit", path="", tool="git",
                risk=result.get("risk_level", "NONE"), capability="release",
                approved=approved)
            event("permission_decision", commit_decision.to_dict())
            if not commit_decision.allowed:
                raise RuntimeError(f"commit not permitted: {commit_decision.reason}")
            stage("COMMIT")
            commit_started = perf_counter()
            commit = git.commit_accepted(touched, "forge: " + requirement, decision)
            if commit.returncode != 0:
                raise RuntimeError(commit.stderr.strip() or "git commit failed")
            timed("commit", commit_started)
            event("commit", {"files": list(touched)})
            checkpoint_manager.cleanup(checkpoint)
            timings["total"] = perf_counter() - started
            result.update(accepted=True, files=touched, model=response.metadata.get("model"),
                          duration_seconds=perf_counter() - started,
                          timings=dict(timings),
                          model_latency_seconds=model_latency,
                          token_usage=dict(token_usage))
            report.stages = list(result["stages"])
            report.agent = "coder"
            report.model = response.metadata.get("model", "")
            report.provider = response.metadata.get("provider", "")
            report.context_fingerprint = context.fingerprint
            report.files_read = files_read
            report.files_changed = touched
            report.commands_run = commands_run
            report.tests_run = len(debug_result.attempts)
            report.test_result = result["test_result"]
            report.review_result = review_decision.to_dict()
            report.security_result = asdict(security)
            report.build_result = asdict(gate_build)
            report.acceptance = decision.to_dict()
            report.retries = len(debug_result.attempts)
            report.duration_seconds = perf_counter() - started
            report.final_status = "COMPLETED"
            report.timings = dict(timings)
            report.model_latency_seconds = model_latency
            report.input_tokens = token_usage["input"]
            report.output_tokens = token_usage["output"]
            result["report"] = report.to_dict()
            stage("COMPLETED")
            return result
        except Exception as exc:
            stage("ROLLBACK")
            event("rollback", {"files": list(touched)})
            # Restore only files belonging to this run. Unrelated user files are
            # not deleted or rewritten. Also remove candidate index entries after
            # a commit/staging failure without touching the worktree.
            git.unstage_files(touched)
            checkpoint_manager.rollback(checkpoint, sorted(set(touched)))
            checkpoint_manager.cleanup(checkpoint)
            timings["total"] = perf_counter() - started
            try:
                history_source = (fabric.router.history if fabric is not None
                                  else router.history if router is not None else [])
            except Exception:
                history_source = []
            model_latency = _history_latency_seconds(history_source)
            token_usage = _history_token_usage(history_source)
            report.stages = list(result["stages"])
            report.files_read = files_read
            report.files_changed = touched
            report.commands_run = commands_run
            report.error = str(exc)
            report.rollback = True
            report.duration_seconds = perf_counter() - started
            report.final_status = "FAILED"
            report.timings = dict(timings)
            report.model_latency_seconds = model_latency
            report.input_tokens = token_usage["input"]
            report.output_tokens = token_usage["output"]
            result.update(error=str(exc), rollback=True, files=touched,
                          failure_reason=str(exc), duration_seconds=perf_counter() - started,
                          timings=dict(timings),
                          model_latency_seconds=model_latency,
                          token_usage=dict(token_usage),
                          report=report.to_dict())
            return result
