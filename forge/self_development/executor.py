import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from forge.agents.registry import AgentRegistry
from forge.agents.selector import AgentSelector
from forge.core.supervisor import Supervisor
from forge.core.task_engine import TaskEngine
from forge.memory.store import MemoryStore
from forge.models.router import ModelRouter
from forge.security.permissions import PermissionLevel, PermissionManager
from forge.self_development.evaluator import CandidateEvaluator, EvaluationResult
from forge.self_development.improvements import ImprovementCandidate
from forge.tools.git import GitTool


class SelfDevelopmentExecutor:
    """Controls the self-development modification, testing, evaluation, and commit/rollback lifecycle."""

    def __init__(
        self,
        root: str | Path = ".",
        supervisor: Optional[Supervisor] = None,
        task_engine: Optional[TaskEngine] = None,
        registry: Optional[AgentRegistry] = None,
        router: Optional[ModelRouter] = None,
        permissions: Optional[PermissionManager] = None,
        memory: Optional[MemoryStore] = None,
        git_tool: Optional[GitTool] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.supervisor = supervisor or Supervisor("forge-self")
        self.task_engine = task_engine or TaskEngine()
        self.registry = registry or AgentRegistry()
        self.selector = AgentSelector(self.registry)
        self.router = router or ModelRouter()
        self.permissions = permissions or PermissionManager()
        self.memory = memory or MemoryStore(root=str(self.root / ".forge" / "memory"))
        self.git_tool = git_tool or GitTool(repo=str(self.root))
        self.evaluator = CandidateEvaluator(root=self.root)

    def execute_candidate(
        self,
        candidate: ImprovementCandidate,
        modifier_fn: Optional[Callable[[ImprovementCandidate], None]] = None,
    ) -> EvaluationResult:
        start_time = time.time()

        # Check permissions before modifying
        perm = self.permissions.check("write_file")
        if perm == PermissionLevel.BLOCKED:
            res = EvaluationResult(
                accepted=False,
                rejection_reason="Permission blocked: write_file operation is blocked",
            )
            return res

        # Step 1: ANALYZE & BASELINE
        baseline_state = self.supervisor.execute_self_development_stage(
            "ANALYZE", lambda: self.evaluator.capture_state()
        )

        # Step 2: CHECKPOINT
        checkpoint_id = f"checkpoint-{candidate.id}"
        initial_commit = self._get_head_commit()

        # Step 3: PLAN
        unique_task_id = f"task-{candidate.id}-{int(time.time() * 1000)}"
        task = self.supervisor.execute_self_development_stage(
            "PLAN",
            lambda: self.task_engine.add(
                task_id=unique_task_id,
                description=f"Self-improvement: {candidate.title}",
            ),
        )

        # Step 4: SELECT AGENTS & MODEL
        selected_agent = self.selector.select("coding")
        selected_model = self.router.select("coding", task_complexity=1.0)

        # Step 5: CODE / EXECUTE MODIFICATION
        def perform_code():
            if modifier_fn:
                modifier_fn(candidate)

        self.supervisor.execute_self_development_stage("CODE", perform_code)

        # Step 6: TEST & BENCHMARK
        candidate_state = self.supervisor.execute_self_development_stage(
            "BENCHMARK", lambda: self.evaluator.capture_state()
        )

        # Step 7: EVALUATE CANDIDATE
        eval_result = self.evaluator.evaluate(baseline_state, candidate_state)

        # Step 8: ACCEPT / COMMIT OR REJECT / ROLLBACK
        if eval_result.accepted:
            self.supervisor.execute_self_development_stage(
                "ACCEPT",
                lambda: self._commit_changes(
                    f"self-dev: {candidate.id} - {candidate.title}"
                ),
            )
        else:
            self.supervisor.execute_self_development_stage(
                "REJECT", lambda: self._rollback_changes(initial_commit)
            )

        duration = time.time() - start_time

        # Record run history
        history_record = {
            "timestamp": time.time(),
            "candidate": candidate.to_dict(),
            "files_changed": candidate.affected_files,
            "baseline_state": baseline_state,
            "candidate_state": candidate_state,
            "accepted": eval_result.accepted,
            "rejection_reason": eval_result.rejection_reason,
            "model_used": selected_model.name if selected_model else "default",
            "agents_used": [selected_agent.name] if selected_agent else [],
            "execution_duration": duration,
        }

        history_dir = self.root / ".forge" / "self" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_file = (
            history_dir / f"run_{candidate.id}_{int(time.time() * 1000)}.json"
        )
        history_file.write_text(json.dumps(history_record, indent=2), encoding="utf-8")

        return eval_result

    def _get_head_commit(self) -> str:
        proc = self.git_tool.run("rev-parse", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _commit_changes(self, message: str) -> None:
        self.git_tool.run("add", ".")
        self.git_tool.run("commit", "-m", message)

    def _rollback_changes(self, initial_commit: str) -> None:
        self.git_tool.run("reset", "--hard", "HEAD")
        self.git_tool.run("clean", "-fd", "-e", ".forge")
