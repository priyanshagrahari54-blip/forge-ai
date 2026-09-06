import json
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from forge.self_development.analyzer import ForgeSelfAnalyzer
from forge.self_development.evaluator import EvaluationResult
from forge.self_development.executor import SelfDevelopmentExecutor
from forge.self_development.history import HistoryStore
from forge.self_development.improvements import (
    CandidateStatus,
    ImprovementCandidate,
    ImprovementGenerator,
)


class SelfDevelopmentLoop:
    """Orchestrates bounded autonomous self-development iterations with history tracking."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.analyzer = ForgeSelfAnalyzer(root=self.root)
        self.executor = SelfDevelopmentExecutor(root=self.root)
        self.history_store = HistoryStore(root=self.root)
        self.stopped = False
        self.iteration_count = 0
        self.results_history: List[EvaluationResult] = []

    def run_once(
        self,
        modifier_fn: Optional[Callable[[ImprovementCandidate], None]] = None,
    ) -> Optional[EvaluationResult]:
        if self.stopped:
            return None

        self.iteration_count += 1

        # 1. Analyze Forge repository
        analysis = self.analyzer.analyze()
        findings = analysis.get("findings", [])

        # 2. Load historical records
        records = self.history_store.list_records()
        history_dicts = [r.to_dict() for r in records]

        # 3. Generate & Rank candidates using SHA-256 identity
        generator = ImprovementGenerator(history=history_dicts)
        candidates = generator.generate(findings)

        if not candidates:
            return None

        # Filter out candidates that have failed repeatedly
        selectable_candidates = [
            c
            for c in candidates
            if not self.history_store.is_candidate_repeatedly_failed(c.candidate_hash)
        ]

        if not selectable_candidates:
            selectable_candidates = candidates

        target_candidate = selectable_candidates[0]

        # 4. Execute candidate self-development cycle
        eval_result = self.executor.execute_candidate(
            target_candidate, modifier_fn=modifier_fn
        )

        self.results_history.append(eval_result)
        return eval_result

    def run(
        self,
        max_iterations: int = 1,
        modifier_fn: Optional[Callable[[ImprovementCandidate], None]] = None,
    ) -> List[EvaluationResult]:
        self.stopped = False
        results: List[EvaluationResult] = []

        safe_max = max(1, min(max_iterations, 100))

        for i in range(safe_max):
            if self.stopped:
                break

            res = self.run_once(modifier_fn=modifier_fn)
            if res is None:
                break
            results.append(res)

        return results

    def stop(self) -> None:
        self.stopped = True

    def status(self) -> dict[str, Any]:
        records = self.history_store.list_records()
        return {
            "root": str(self.root),
            "iteration_count": self.iteration_count,
            "stopped": self.stopped,
            "total_runs_in_history": len(records),
            "accepted_runs": sum(1 for r in records if r.accepted),
            "rejected_runs": sum(1 for r in records if not r.accepted),
        }
