"""Prompt-strategy learning (A84 Stage I3).

Records which prompt *strategy* (enhancement emphasis) was applied for a task
type, which model served it, and the outcome/evaluation. Future enhancement
consults accumulated evidence — but only when the evidence is real:

* a strategy wins only with at least ``MIN_SAMPLES`` recorded outcomes;
* below that floor the strategy ledger returns ``None`` and the caller uses
  the default — *no evidence, no claim of learning*;
* strategies change emphasis only. They can never alter capabilities,
  verification requirements or policy — the pipeline renders them as a single
  guidance line.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

__all__ = ["PromptStrategyLedger", "STRATEGIES"]

#: The closed vocabulary of enhancement strategies (matches pipeline render).
STRATEGIES = ("default", "stepwise", "outline-first", "evidence-first",
              "test-first")

MIN_SAMPLES = 5          # outcomes before a strategy may influence selection
MAX_ROWS = 20_000
#: Strategies that may be suggested, never ones that were harmful: a strategy
#: whose success rate is below the floor is *disqualified*, not "learned".
DISQUALIFY_RATE = 0.20


class PromptStrategyLedger:
    """SQLite-backed prompt-strategy outcome ledger."""

    def __init__(self, db: Any, *, min_samples: int = MIN_SAMPLES) -> None:
        self._db = db
        self.min_samples = max(2, int(min_samples))
        db.execute("""
            CREATE TABLE IF NOT EXISTS prompt_strategy_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                strategy TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL,          -- success | failure | rejected
                evaluation REAL NOT NULL DEFAULT 0.0,
                note TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )""")
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_strategy_lookup
            ON prompt_strategy_outcomes (task_type, strategy)""")

    # -- write -----------------------------------------------------------------

    def record(self, task_type: str, strategy: str, *, outcome: str,
               model: str = "", evaluation: float = 0.0, note: str = "") -> Dict[str, Any]:
        task_type = (task_type or "").strip().lower()[:48] or "general"
        strategy = (strategy or "").strip().lower()[:32]
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown prompt strategy {strategy!r}; "
                             f"expected one of {STRATEGIES}")
        outcome = (outcome or "").strip().lower()[:16]
        if outcome not in ("success", "failure", "rejected"):
            raise ValueError("outcome must be success|failure|rejected")
        self._db.execute(
            "INSERT INTO prompt_strategy_outcomes (task_type, strategy, "
            "model, outcome, evaluation, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_type, strategy, model[:120], outcome,
             max(0.0, min(1.0, float(evaluation or 0.0))), note[:280],
             time.time()))
        self._prune()
        return self.stats(task_type=task_type, strategy=strategy)

    # -- read ------------------------------------------------------------------

    def stats(self, *, task_type: str = "", strategy: str = "") -> Dict[str, Any]:
        where: List[str] = []
        params: List[Any] = []
        if task_type:
            where.append("task_type = ?")
            params.append(task_type.strip().lower()[:48])
        if strategy:
            where.append("strategy = ?")
            params.append(strategy.strip().lower()[:32])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self._db.query(
            "SELECT strategy, COUNT(*) AS total, "
            "SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS ok, "
            "AVG(evaluation) AS avg_eval "
            f"FROM prompt_strategy_outcomes {clause} GROUP BY strategy",
            tuple(params))
        strategies = {}
        for row in rows:
            total = int(row["total"] or 0)
            ok = int(row["ok"] or 0)
            strategies[row["strategy"]] = {
                "samples": total,
                "success_rate": round(ok / total, 4) if total else 0.0,
                "avg_evaluation": round(float(row["avg_eval"] or 0.0), 4),
                "established": total >= self.min_samples,
            }
        return {"task_type": task_type, "min_samples": self.min_samples,
                "strategies": strategies}

    def best_strategy(self, task_type: str) -> Optional[str]:
        """The best established strategy for *task_type*, or None.

        None means "no evidence yet" — callers must then use the default
        rendering. A strategy below :data:`DISQUALIFY_RATE` is never
        returned, so the ledger can only lift behaviour, never poison it.
        """
        stats = self.stats(task_type=task_type)
        candidates = [
            (data["success_rate"], data["avg_evaluation"], name)
            for name, data in stats["strategies"].items()
            if data["established"]
            and data["success_rate"] >= DISQUALIFY_RATE
            and name != "default"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return candidates[0][2]

    def disqualify_reason(self, task_type: str, strategy: str) -> str:
        stats = self.stats(task_type=task_type, strategy=strategy)
        data = (stats["strategies"] or {}).get(strategy)
        if data is None:
            return "no recorded outcomes"
        if not data["established"]:
            return (f"only {data['samples']} samples "
                    f"(< {self.min_samples}); no learning claim")
        if data["success_rate"] < DISQUALIFY_RATE:
            return (f"success rate {data['success_rate']:.0%} below the "
                    f"{DISQUALIFY_RATE:.0%} floor")
        return ""

    # -- bounds ----------------------------------------------------------------

    def _prune(self) -> None:
        row = self._db.query_one(
            "SELECT COUNT(*) AS total FROM prompt_strategy_outcomes")
        total = int(row["total"]) if row is not None else 0
        overflow = total - MAX_ROWS
        if overflow > 0:
            self._db.execute(
                "DELETE FROM prompt_strategy_outcomes WHERE id IN ("
                "SELECT id FROM prompt_strategy_outcomes "
                "ORDER BY id ASC LIMIT ?)", (overflow,))
