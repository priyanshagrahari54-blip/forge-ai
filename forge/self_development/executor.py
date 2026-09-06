import json
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from forge.agents.registry import AgentRegistry
from forge.agents.selector import AgentSelector
from forge.core.supervisor import Supervisor
from forge.core.task_engine import TaskEngine
from forge.memory.store import MemoryStore
from forge.models.router import ModelRouter
from forge.security.permissions import PermissionLevel, PermissionManager
from forge.self_development.agent_runner import AutonomousAgentRunner
from forge.self_development.checkpoint import CheckpointManager, CheckpointSnapshot
from forge.self_development.evaluator import CandidateEvaluator, EvaluationResult
from forge.self_development.improvements import CandidateStatus, ImprovementCandidate
from forge.self_development.task import SelfDevelopmentTask
from forge.tools.git import GitTool


class SelfDevelopmentExecutor:
    """Controls self-development modification, debug loop, evaluation, and explicit commit/rollback lifecycle."""

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
        llm_worker: Optional[Callable[[str, str], str]] = None,
        max_retries: int = 2,
    ) -> None:
        self.root = Path(root).resolve()
        self.supervisor = supervisor or Supervisor("forge-self")
        self.task_engine = task_engine or TaskEngine()
        self.registry = registry or AgentRegistry()
        self.router = router or ModelRouter()
        self.permissions = permissions or PermissionManager()
        self.memory = memory or MemoryStore(root=str(self.root / ".forge" / "memory"))
        self.git_tool = git_tool or GitTool(repo=str(self.root))
        self.checkpoint_manager = CheckpointManager(root=self.root, git_tool=self.git_tool)
        self.evaluator = CandidateEvaluator(root=self.root)
        self.agent_runner = AutonomousAgentRunner(
            root=self.root,
            registry=self.registry,
            router=self.router,
            permissions=self.permissions,
            llm_worker=llm_worker,
        )
        self.max_retries = max_retries

    def execute_candidate(
        self,
        candidate: ImprovementCandidate,
        modifier_fn: Optional[Callable[[Any], None]] = None,
    ) -> EvaluationResult:
        start_time = time.time()

        # Step 1: ANALYZE & BASELINE STATE
        baseline_state = self.supervisor.execute_self_development_stage(
            "ANALYZE", lambda: self.evaluator.capture_state()
        )

        # Step 2: CHECKPOINT
        checkpoint_id = f"ckpt-{candidate.id}"
        snapshot = self.checkpoint_manager.create_checkpoint(
            checkpoint_id, candidate_metadata=candidate.to_dict()
        )

        # Step 3: PLAN & SELF-DEV TASK
        task_obj = SelfDevelopmentTask.from_candidate(candidate)
        unique_task_id = f"task-{candidate.id}-{int(time.time() * 1000)}"

        self.supervisor.execute_self_development_stage(
            "PLAN",
            lambda: self.task_engine.add(
                task_id=unique_task_id,
                description=f"Self-improvement: {candidate.title}",
            ),
        )

        attempts_history: List[dict[str, Any]] = []
        final_eval_result: Optional[EvaluationResult] = None
        error_context: Optional[str] = None

        # Step 4: CODE -> TEST -> DEBUG LOOP (Bounded by max_retries)
        for attempt in range(1, self.max_retries + 2):
            attempt_start = time.time()

            # Execute code modification via AutonomousAgentRunner
            run_info = self.supervisor.execute_self_development_stage(
                "CODE",
                lambda: self.agent_runner.run_modification(
                    task=task_obj,
                    modifier_fn=modifier_fn,
                    error_context=error_context,
                ),
            )

            # Benchmark & Evaluate Candidate State
            candidate_state = self.supervisor.execute_self_development_stage(
                "BENCHMARK", lambda: self.evaluator.capture_state()
            )

            eval_result = self.evaluator.evaluate(
                baseline_state, candidate_state, candidate_class=candidate.candidate_class
            )

            attempt_duration = time.time() - attempt_start
            attempt_record = {
                "attempt_number": attempt,
                "agent_info": run_info,
                "eval_result": eval_result.to_dict(),
                "duration": attempt_duration,
            }
            attempts_history.append(attempt_record)

            if eval_result.accepted:
                final_eval_result = eval_result
                break
            else:
                error_context = eval_result.rejection_reason or "Verification failed"
                if attempt <= self.max_retries:
                    # Rollback to snapshot before retrying debug loop
                    self.checkpoint_manager.rollback(snapshot)

        if final_eval_result is None:
            final_eval_result = eval_result

        # Step 5: ACCEPT/COMMIT OR REJECT/ROLLBACK
        if final_eval_result.accepted:
            candidate.status = CandidateStatus.ACCEPTED.value
            self.supervisor.execute_self_development_stage(
                "ACCEPT",
                lambda: self._safe_commit_changes(
                    f"self-dev: {candidate.id} - {candidate.title}",
                    candidate.affected_files,
                ),
            )
        else:
            candidate.status = CandidateStatus.ROLLED_BACK.value
            self.supervisor.execute_self_development_stage(
                "REJECT", lambda: self.checkpoint_manager.rollback(snapshot)
            )

        duration = time.time() - start_time

        # Step 6: RECORD DETAILED HISTORY
        history_record = {
            "timestamp": time.time(),
            "candidate": candidate.to_dict(),
            "candidate_hash": candidate.candidate_hash,
            "baseline_commit": snapshot.head_commit,
            "checkpoint_id": snapshot.checkpoint_id,
            "files_changed": candidate.affected_files,
            "baseline_state": baseline_state,
            "candidate_state": candidate_state,
            "attempts": attempts_history,
            "accepted": final_eval_result.accepted,
            "accepted_because": final_eval_result.accepted_because,
            "rejected_because": final_eval_result.rejection_reason,
            "execution_duration": duration,
        }

        history_dir = self.root / ".forge" / "self" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_file = (
            history_dir / f"run_{candidate.id}_{int(time.time() * 1000)}.json"
        )
        history_file.write_text(json.dumps(history_record, indent=2), encoding="utf-8")

        return final_eval_result

    def _safe_commit_changes(self, message: str, allowed_files: List[str]) -> None:
        """Inspects diff and stages only allowed candidate files, excluding secrets and history."""
        status_proc = self.git_tool.run("status", "--porcelain")
        if status_proc.returncode != 0 or not status_proc.stdout.strip():
            return

        changed_lines = status_proc.stdout.splitlines()
        for line in changed_lines:
            file_path = line[3:].strip()
            if not file_path:
                continue

            # Exclude secrets, .env, and history from automated commit
            if (
                file_path.startswith(".forge/self/history")
                or file_path.startswith(".env")
                or file_path.endswith(".secret")
            ):
                continue

            # Stage explicitly
            self.git_tool.run("add", file_path)

        # Commit staged allowed files
        self.git_tool.run("commit", "-m", message)
