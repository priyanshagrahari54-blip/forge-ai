from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from forge.agents.coder import CoderAgent
from forge.agents.debugger import DebuggerAgent, TestDebugLoop
from forge.agents.execution import AgentRequest
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.core.supervisor import Supervisor
from forge.intelligence.repository import RepositoryIntelligence
from forge.models.provider import LocalModelProvider
from forge.models.router import ModelInfo, ModelRouter
from forge.security.verification import VerificationPipeline
from forge.self_development.evaluator import CandidateEvaluator, EvaluationResult
from forge.self_development.improvements import ImprovementCandidate
from forge.tools.checkpoint import CheckpointManager
from forge.tools.git import GitTool


class SelfDevelopmentExecutor:
    """Run A26-A30 candidates through the same guarded coding infrastructure."""

    def __init__(self, root=".", supervisor=None, task_engine=None, registry=None,
                 router=None, permissions=None, memory=None, git_tool=None):
        self.root = Path(root).resolve()
        self.supervisor = supervisor or Supervisor("forge-self", self.root)
        self.task_engine = task_engine or TaskEngine()
        self.router = router or ModelRouter([ModelInfo(
            "local", "coding", available=True, free=True, provider=LocalModelProvider(),
            capabilities=("coding", "debugging"),
        )])
        self.evaluator = CandidateEvaluator(self.root)
        self.git_tool = git_tool or GitTool(str(self.root))
        self.checkpoints = CheckpointManager(self.root)
        self.coder = CoderAgent(root=str(self.root), router=self.router)
        self.verifier = VerificationPipeline(self.root)

    def execute_candidate(self, candidate: ImprovementCandidate,
                          modifier_fn: Optional[Callable] = None) -> EvaluationResult:
        """Execute a candidate. ``modifier_fn`` is a legacy test-only hook.

        Autonomous callers omit it; the normal path always asks the routed model
        for a structured change and writes through CoderAgent/ToolRuntime.
        """
        started = time.perf_counter()
        baseline = self.evaluator.capture_state()
        checkpoint = self.checkpoints.create(candidate.id)
        task = self.task_engine.add(
            f"self-{candidate.id}-{int(time.time() * 1000)}",
            f"Self-improvement: {candidate.title}",
        )
        task.status = TaskStatus.CODING
        touched = list(candidate.affected_files)
        result: EvaluationResult
        try:
            if modifier_fn is not None:
                modifier_fn(candidate)
            else:
                intelligence = RepositoryIntelligence.build(self.root)
                context = self.coder.build_context(
                    intelligence, candidate.proposed_improvement, tuple(candidate.affected_files)
                )
                response = self.coder.execute(AgentRequest(
                    task=task, stage=TaskStatus.CODING, context=context,
                    instructions=candidate.proposed_improvement, metadata={"approved": True},
                ))
                touched = list(response.metadata.get("files", []))
                if not response.success:
                    raise RuntimeError(response.error or "model coding failed")

            debugger = DebuggerAgent(str(self.root), router=self.router)
            debug_result = TestDebugLoop(self.root, max_retries=3, debugger=debugger).run(
                candidate.proposed_improvement, approved=True
            )
            touched = sorted(set(touched) | {
                path for attempt in debug_result.attempts for path in attempt.modifications
            })
            diff = self.git_tool.diff() + "\n" + self.git_tool.status()
            verification = self.verifier.run(diff, touched)
            state = self.evaluator.capture_state()
            if not debug_result.success or not verification.passed:
                state["passed_benchmarks"] = 0
                state["verification_failures"] = [gate.details for gate in verification.failures]
            result = self.evaluator.evaluate(baseline, state, require_improvement=modifier_fn is None)
            if result.accepted and self._is_git_repo() and touched:
                commit = self.git_tool.commit_files(touched, f"self-dev: {candidate.id} - {candidate.title}")
                if commit.returncode:
                    self.git_tool.unstage_files(touched)
                    result.accepted = False
                    result.rejection_reason = commit.stderr.strip() or "git commit failed"
            if result.accepted:
                self.checkpoints.cleanup(checkpoint)
            else:
                self.checkpoints.rollback(checkpoint, touched)
                self.checkpoints.cleanup(checkpoint)
        except Exception as exc:
            self.git_tool.unstage_files(touched)
            self.checkpoints.rollback(checkpoint, touched)
            self.checkpoints.cleanup(checkpoint)
            result = EvaluationResult(accepted=False, rejection_reason=str(exc))

        record = {
            "timestamp": time.time(), "candidate": candidate.to_dict(),
            "accepted": result.accepted, "rejection_reason": result.rejection_reason,
            "duration": time.perf_counter() - started, "files_changed": touched,
            "router_history": self.router.history[-20:], "status": self.supervisor.current_stage,
        }
        history = self.root / ".forge" / "self" / "history"
        history.mkdir(parents=True, exist_ok=True)
        (history / f"run_{candidate.id}_{int(time.time() * 1000)}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        return result

    def _is_git_repo(self) -> bool:
        return self.git_tool.run("rev-parse", "--is-inside-work-tree").returncode == 0
