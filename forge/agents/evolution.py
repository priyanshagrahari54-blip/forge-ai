"""Agent evolution (A50): evidence snapshots from real run outcomes.

No fake learning: evolution records *real* terminal run outcomes
(status, attempts, elapsed time) against an agent definition and
bumps its generation counter exactly once per recorded outcome.
Metrics are computed from those records — success rates are the real
fraction of recorded runs, and an agent with zero recorded outcomes
has no metrics at all.

The ledger keeps the recent outcome history (most recent last), so
promotion/retirement decisions can inspect the *actual last N
outcomes* — never a single ``last_outcome`` shortcut. The reusable
helpers below are the single source of truth for those rules:

- ``consecutive_failures(history)`` — trailing FAILED count
- ``recent_outcomes(history, window)`` — last ``window`` outcomes
- ``promotion_eligible(metrics, ...)`` — real windowed criteria
- ``retirement_eligible(metrics, ...)`` — real threshold criteria

Full lifecycle: train → benchmark → promote → retire, driven by real
metrics and real training pipelines (``forge.agents.training``), which
collect real examples from runs and only export/upload them through a
fail-closed ``TrainingDataPolicy``.
"""
from __future__ import annotations

from typing import Any, Sequence

TERMINAL_FAILED = "FAILED"
TERMINAL_SUCCEEDED = "SUCCEEDED"
HISTORY_LIMIT = 20

_OUTCOME_STATUSES = {TERMINAL_FAILED, TERMINAL_SUCCEEDED,
                     "CANCELLED", "ROLLED_BACK"}


def _is_failure(outcome: str) -> bool:
    return str(outcome) == TERMINAL_FAILED


def consecutive_failures(history: Sequence[str]) -> int:
    """Number of trailing consecutive FAILED outcomes in ``history``.

    ``[S, F, F]`` → 2; ``[F]`` → 1; ``[]`` → 0; ``[F, F, S]`` → 0.
    This is what "retire after 5 consecutive failures" must check:
    five FAILED rows in a row, not merely ``runs >= 5 and last ==
    FAILED``.
    """
    count = 0
    for outcome in reversed(list(history)):
        if _is_failure(outcome):
            count += 1
        else:
            break
    return count


def recent_outcomes(history: Sequence[str], window: int) -> list[str]:
    """Last ``window`` outcomes (oldest first), at most ``window`` items."""
    return list(history)[-int(window):]


def promotion_eligible(metrics: dict[str, Any], *,
                       min_runs: int = 5,
                       min_success_rate: float = 0.8,
                       window: int = 3) -> dict[str, Any]:
    """Promotion criteria over *real* outcome history.

    Requires (all):

    - at least ``min_runs`` recorded runs
    - success rate >= ``min_success_rate``
    - the last ``window`` outcomes contain NO two adjacent FAILED
      outcomes — i.e. no consecutive failures in the last ``window``
      runs (a single trailing failure is not consecutive and does not
      block promotion by itself)

    Returns a machine-readable decision with per-criterion evidence.
    """
    history = [str(o) for o in (metrics.get("outcome_history") or [])]
    runs = int(metrics.get("runs", 0) or 0)
    success_rate = float(metrics.get("success_rate", 0.0) or 0.0)
    windowed = recent_outcomes(history, window)

    min_runs_ok = runs >= min_runs
    success_rate_ok = success_rate >= min_success_rate
    # An agent with NO recorded outcome history can never be eligible,
    # even if someone passes hand-built aggregate counters.
    has_history = bool(history)
    no_consecutive = all(
        not (_is_failure(earlier) and _is_failure(later))
        for earlier, later in zip(windowed, windowed[1:]))
    trailing = consecutive_failures(windowed)

    criteria = {
        "min_runs": min_runs_ok,
        "success_rate": success_rate_ok,
        "recorded_history": has_history,
        "no_consecutive_failures_in_window": no_consecutive,
        "evidence": {
            "runs": runs,
            "success_rate": success_rate,
            "window_outcomes": windowed,
            "window_size": window,
            "window_available": bool(windowed),
            "trailing_consecutive_failures": trailing,
        },
    }
    return {"eligible": bool(all((
        min_runs_ok, success_rate_ok, has_history, no_consecutive))),
        "criteria": criteria}


def retirement_eligible(metrics: dict[str, Any], *,
                        min_runs: int = 10,
                        max_success_rate: float = 0.3,
                        consecutive_threshold: int = 5) -> dict[str, Any]:
    """Retirement criteria over *real* outcome history.

    Retires when (either):

    - at least ``min_runs`` recorded runs AND success rate below
      ``max_success_rate``; or
    - ``consecutive_threshold`` FAILED outcomes in a row (verified
      against the actual trailing history — not ``runs >= N and
      last == FAILED``).

    Returns a machine-readable decision with per-criterion evidence.
    """
    history = [str(o) for o in (metrics.get("outcome_history") or [])]
    runs = int(metrics.get("runs", 0) or 0)
    success_rate = float(metrics.get("success_rate", 0.0) or 0.0)
    trailing = consecutive_failures(history)

    low_success = runs >= min_runs and success_rate < max_success_rate
    consecutive = trailing >= consecutive_threshold and \
        len(history) >= consecutive_threshold
    criteria = {
        "low_success_rate": bool(low_success),
        "consecutive_failures": bool(consecutive),
        "evidence": {
            "runs": runs,
            "success_rate": success_rate,
            "outcome_history": history[-max(consecutive_threshold, 5):],
            "trailing_consecutive_failures": trailing,
            "threshold": consecutive_threshold,
        },
    }
    return {"eligible": bool(low_success or consecutive),
            "criteria": criteria}


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
            "outcome_history": [],
        })
        started = run.started_at or run.created_at
        finished = run.finished_at or run.updated_at
        elapsed_ms = max(0.0, (finished - started) * 1000.0)
        ledger["runs"] += 1
        ledger["attempts"] += max(1, int(run.attempts or 1))
        ledger["total_elapsed_ms"] = round(
            ledger["total_elapsed_ms"] + elapsed_ms, 2)
        outcome = str(run.status)
        if outcome == TERMINAL_SUCCEEDED:
            ledger["succeeded"] += 1
        else:
            ledger["failed"] += 1
        ledger["last_outcome"] = outcome
        ledger["outcome_history"] = (
            ledger["outcome_history"][-(HISTORY_LIMIT - 1):] + [outcome])
        definition.generation += 1
        definition.metrics = self.snapshot(definition.name)
        return definition.metrics

    def snapshot(self, agent_name: str) -> dict[str, Any]:
        ledger = self._ledger.get(agent_name)
        if ledger is None:
            return {}
        runs = ledger["runs"]
        history = list(ledger.get("outcome_history") or [])
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
            #: Real outcome history (oldest first, most recent last).
            "outcome_history": history,
            "recent_outcomes": history[-3:],
            "consecutive_failures": consecutive_failures(history),
        }

    def ledger(self, agent_name: str) -> dict[str, Any] | None:
        return self._ledger.get(agent_name)
