from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from forge.self_development.analyzer import ForgeSelfAnalyzer
from forge.self_development.evaluator import EvaluationResult
from forge.self_development.executor import SelfDevelopmentExecutor
from forge.self_development.improvements import (
    ImprovementCandidate,
    ImprovementGenerator,
)


class SelfDevelopmentLoop:
    """Orchestrates bounded autonomous self-development iterations."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.analyzer = ForgeSelfAnalyzer(root=self.root)
        self.executor = SelfDevelopmentExecutor(root=self.root)
        self.stopped = False
        self.iteration_count = 0
        self.results_history: list[EvaluationResult] = []

    def load_history(self) -> list[dict[str, Any]]:
        history_dir = self.root / ".forge" / "self" / "history"
        if not history_dir.exists():
            return []

        records: list[dict[str, Any]] = []
        for file in sorted(history_dir.glob("run_*.json")):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                records.append(data)
            except Exception:
                continue
        return records

    def run_once(
        self,
        modifier_fn: Optional[Callable[[ImprovementCandidate], None]] = None,
    ) -> Optional[EvaluationResult]:
        if self.stopped:
            return None

        self.iteration_count += 1

        # 1. Analyze Forge
        analysis = self.analyzer.analyze()
        findings = analysis.get("findings", [])

        # 2. Load historical memory
        history = self.load_history()

        # 3. Generate & Rank candidates
        generator = ImprovementGenerator(history=history)
        candidates = generator.generate(findings)

        if not candidates:
            return None

        # Filter out candidates that were recently attempted or failed if needed
        attempted_candidate_ids = {
            rec.get("candidate", {}).get("id") for rec in history
        }

        selectable_candidates = [
            c for c in candidates if c.id not in attempted_candidate_ids
        ]
        if not selectable_candidates:
            selectable_candidates = candidates

        target_candidate = selectable_candidates[0]

        # 4. Execute candidate cycle
        eval_result = self.executor.execute_candidate(
            target_candidate, modifier_fn=modifier_fn
        )

        self.results_history.append(eval_result)
        return eval_result

    def run(
        self,
        max_iterations: int = 1,
        modifier_fn: Optional[Callable[[ImprovementCandidate], None]] = None,
    ) -> list[EvaluationResult]:
        self.stopped = False
        results: list[EvaluationResult] = []

        safe_max = max(1, min(max_iterations, 100))  # Enforce hard upper boundary limit

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
        history = self.load_history()
        return {
            "root": str(self.root),
            "iteration_count": self.iteration_count,
            "stopped": self.stopped,
            "total_runs_in_history": len(history),
            "accepted_runs": sum(1 for h in history if h.get("accepted", False)),
            "rejected_runs": sum(1 for h in history if not h.get("accepted", False)),
        }
