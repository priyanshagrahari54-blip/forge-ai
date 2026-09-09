"""Agent evolution (A50): evidence snapshots from real run outcomes.

No fake learning: evolution records *real* terminal run outcomes
(status, attempts, elapsed time) against an agent definition and
bumps its generation counter exactly once per recorded outcome.
Metrics are computed from those records — success rates are the real
fraction of recorded runs, and an agent with zero recorded outcomes
has no metrics at all.
"""
from __future__ import annotations

from typing import Any


class AgentEvolution:
    """Per-session evolution ledger keyed by agent name."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._ledger: dict[str, dict[str, Any]] = {}

    def record(self, definition: Any, run: Any) -> dict[str, Any]:
        """Incorporate one real terminal run into the agent's ledger."""
        ledger = self._ledger.setdefault(definition.name, {
            "runs": 0, "succeeded": 0, "failed": 0, "attempts": 0,
            "total_elapsed_ms": 0.0, "last_outcome": "",
        })
        started = run.started_at or run.created_at
        finished = run.finished_at or run.updated_at
        elapsed_ms = max(0.0, (finished - started) * 1000.0)
        ledger["runs"] += 1
        ledger["attempts"] += max(1, int(run.attempts or 1))
        ledger["total_elapsed_ms"] = round(
            ledger["total_elapsed_ms"] + elapsed_ms, 2)
        outcome = str(run.status)
        if outcome == "SUCCEEDED":
            ledger["succeeded"] += 1
        else:
            ledger["failed"] += 1
        ledger["last_outcome"] = outcome
        definition.generation += 1
        definition.metrics = self.snapshot(definition.name)
        return definition.metrics

    def snapshot(self, agent_name: str) -> dict[str, Any]:
        ledger = self._ledger.get(agent_name)
        if ledger is None:
            return {}
        runs = ledger["runs"]
        return {
            "runs": runs,
            "succeeded": ledger["succeeded"],
            "failed": ledger["failed"],
            "success_rate": round(ledger["succeeded"] / runs, 3)
            if runs else 0.0,
            "avg_attempts": round(ledger["attempts"] / runs, 3)
            if runs else 0.0,
            "avg_elapsed_ms": round(ledger["total_elapsed_ms"] / runs, 1)
            if runs else 0.0,
            "last_outcome": ledger["last_outcome"],
        }

    def ledger(self, agent_name: str) -> dict[str, Any] | None:
        return self._ledger.get(agent_name)
