"""A50 evolution criteria tests (Phase 6): consecutive-failure rules.

The documented rules ("no consecutive failures in the last 3 runs",
"retire after 5 consecutive failures") must be verified against the
REAL trailing outcome history — never against ``runs >= N and
last_outcome == FAILED``.
"""
from __future__ import annotations

import pytest

from forge.agents.evolution import (
    AgentEvolution,
    consecutive_failures,
    promotion_eligible,
    recent_outcomes,
    retirement_eligible,
)

S = "SUCCEEDED"
F = "FAILED"


# ---------------------------------------------------------------------------
# consecutive_failures — pure trailing-count semantics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("history,expected", [
    ([], 0),
    ([S], 0),
    ([F], 1),
    ([S, F], 1),
    ([F, S], 0),
    ([F, F], 2),
    ([F, F, F], 3),
    ([S, F, F, F], 3),
    ([F, F, S], 0),
    ([F, S, F], 1),
    ([S, S, S], 0),
    ([F] * 5, 5),
    ([F] * 5 + [S], 0),
    ([S] + [F] * 6, 6),
])
def test_consecutive_failures(history, expected):
    assert consecutive_failures(history) == expected


def test_recent_outcomes_window():
    history = [S, F, S, F, F]
    assert recent_outcomes(history, 3) == [S, F, F]
    assert recent_outcomes(history, 10) == history
    assert recent_outcomes([], 3) == []


# ---------------------------------------------------------------------------
# promotion_eligible — windowed criteria on real history
# ---------------------------------------------------------------------------

def _metrics(history, success_rate=None, runs=None):
    runs = runs if runs is not None else len(history)
    return {
        "runs": runs,
        "success_rate": success_rate if success_rate is not None
        else round((history.count(S)) / max(1, len(history)), 3),
        "outcome_history": list(history),
        "last_outcome": history[-1] if history else "",
    }


def test_promotion_requires_min_runs():
    decision = promotion_eligible(_metrics([S, S, S, S]))
    assert decision["eligible"] is False
    assert decision["criteria"]["min_runs"] is False


def test_promotion_requires_success_rate():
    decision = promotion_eligible(_metrics([S, S, S, F, F]))
    assert decision["eligible"] is False
    assert decision["criteria"]["success_rate"] is False


def test_promotion_exactly_at_threshold_succeeds():
    # 5 runs, 80% success, last 3 = S, S, F (no consecutive failure).
    history = [S, S, S, S, F]
    decision = promotion_eligible(_metrics(history))
    assert decision["eligible"] is True
    evidence = decision["criteria"]["evidence"]
    assert evidence["runs"] == 5
    assert evidence["success_rate"] == 0.8


def test_promotion_threshold_minus_one_run_fails():
    # Only 4 runs: the min_runs criterion must fail.
    history = [S, S, S, F]
    decision = promotion_eligible(_metrics(history))
    assert decision["eligible"] is False
    assert decision["criteria"]["min_runs"] is False


@pytest.mark.parametrize("history", [
    [S, S, S, S, S, S, S, S, F, F],  # F,F inside the last-3 window
    [S, S, S, S, S, S, S, S, F, F,
     F],                              # 3 consecutive failures at the tail
    [F, F, F, F, F, F],              # all failing
])
def test_promotion_blocked_by_consecutive_failures_in_window(history):
    metrics = _metrics(history)
    decision = promotion_eligible(metrics)
    assert decision["eligible"] is False
    criteria = decision["criteria"]
    assert criteria["min_runs"] is True
    assert criteria["no_consecutive_failures_in_window"] is False


def test_promotion_window_still_contains_failures_after_interruption():
    # F,F then S: the trailing streak breaks, but the last-3 window is
    # [F,F,S] and still CONTAINS the consecutive F,F pair — promotion
    # must stay blocked (the rule inspects the window, not the tail).
    history = [S, S, S, S, S, S, S, F, F, S]
    metrics = _metrics(history, success_rate=0.8)
    decision = promotion_eligible(metrics)
    assert decision["eligible"] is False
    assert decision["criteria"]["no_consecutive_failures_in_window"] \
        is False
    evidence = decision["criteria"]["evidence"]
    assert evidence["trailing_consecutive_failures"] == 0
    assert evidence["window_outcomes"] == [F, F, S]


def test_promotion_early_failure_streak_interrupted_allows():
    # F,F,S,S,S: the consecutive pair sits before the window and the
    # tail is clean — promotion proceeds when the other criteria hold.
    history = [S, S, S, S, S, F, F, S, S, S]
    metrics = _metrics(history, success_rate=0.8)
    decision = promotion_eligible(metrics)
    assert decision["eligible"] is True
    evidence = decision["criteria"]["evidence"]
    assert evidence["trailing_consecutive_failures"] == 0
    assert evidence["window_outcomes"] == [S, S, S]


def test_promotion_insufficient_history_never_passes_window():
    # 1 failure or 2 failures alone can never promote (window evidence
    # cannot contain consecutive failures but min_runs already blocks).
    for history in ([F], [F, F]):
        decision = promotion_eligible(_metrics(history))
        assert decision["eligible"] is False
        assert decision["criteria"]["min_runs"] is False


def test_promotion_mixed_outcomes_history():
    # Early failures are overcome by a long clean tail.
    history = [F, S, S, S, F, S, S, S, S]
    decision = promotion_eligible(_metrics(history, success_rate=0.9))
    assert decision["eligible"] is True
    assert decision["criteria"]["evidence"]["window_outcomes"] == [S, S, S]


def test_promotion_without_history_never_eligible():
    decision = promotion_eligible(_metrics([], runs=5,
                                           success_rate=0.9))
    assert decision["eligible"] is False


# ---------------------------------------------------------------------------
# retirement_eligible — five real FAILED rows, not a shortcut
# ---------------------------------------------------------------------------

def test_retirement_exactly_five_consecutive_failures():
    metrics = _metrics([S, F, F, F, F, F])
    decision = retirement_eligible(metrics)
    assert decision["eligible"] is True
    assert decision["criteria"]["consecutive_failures"] is True


def test_retirement_threshold_minus_one_does_not_retire():
    # runs >= 5 with last == FAILED must NOT trigger consecutive
    # retirement: only 4 consecutive failures are on record.
    metrics = _metrics([S, S, F, F, F, F])
    decision = retirement_eligible(metrics)
    assert decision["eligible"] is False
    assert decision["criteria"]["consecutive_failures"] is False


def test_retirement_success_interrupts_streak():
    metrics = _metrics([F, F, F, F, S, F])
    decision = retirement_eligible(metrics)
    assert decision["eligible"] is False
    assert decision["criteria"]["consecutive_failures"] is False


def test_retirement_one_failure_only():
    assert retirement_eligible(_metrics([S, S, S, S, F]))["eligible"] \
        is False


def test_retirement_low_success_rate_over_min_runs():
    # 10 runs with only 2 successes: low-success-rate criterion.
    history = [S, S] + [F] * 8
    metrics = _metrics(history, success_rate=0.2, runs=10)
    decision = retirement_eligible(metrics)
    assert decision["eligible"] is True
    assert decision["criteria"]["low_success_rate"] is True


def test_retirement_insufficient_history_not_retired():
    metrics = _metrics([F] * 4, success_rate=0.0, runs=4)
    assert retirement_eligible(metrics)["eligible"] is False
    metrics = _metrics([F] * 5, success_rate=0.0, runs=5)
    assert retirement_eligible(metrics)["eligible"] is True


def test_retirement_mixed_outcomes_low_rate():
    history = [S, F, F, S, F, F, F, F, F, F]
    decision = retirement_eligible(_metrics(history, success_rate=0.2,
                                            runs=10))
    assert decision["eligible"] is True


def test_retirement_uses_recorded_aggregates_without_history():
    # Retirement may fire from recorded aggregates (10+ runs, success
    # rate < 0.3) even when the history list is absent, but the
    # consecutive-failure criterion must show zero evidence.
    decision = retirement_eligible(_metrics([], runs=20,
                                            success_rate=0.1))
    assert decision["eligible"] is True
    assert decision["criteria"]["low_success_rate"] is True
    assert decision["criteria"]["consecutive_failures"] is False
    assert decision["criteria"]["evidence"]["trailing_consecutive_failures"] \
        == 0


def test_retirement_no_history_short_runs():
    # Without recorded history AND without the aggregate precondition,
    # nothing retires.
    assert retirement_eligible(_metrics([], runs=4,
                                        success_rate=0.0))["eligible"] \
        is False


# ---------------------------------------------------------------------------
# ledger records history; snapshot exposes it
# ---------------------------------------------------------------------------

class FakeDefinition:
    def __init__(self, name):
        self.name = name
        self.generation = 1
        self.metrics = {}


class FakeRun:
    def __init__(self, status, attempts=1):
        self.status = status
        self.attempts = attempts
        self.started_at = 1000.0
        self.finished_at = 1002.0
        self.created_at = 1000.0
        self.updated_at = 1002.0


def test_ledger_tracks_outcome_history():
    evolution = AgentEvolution("s1")
    definition = FakeDefinition("worker")
    for status in (S, S, F, F, F):
        evolution.record(definition, FakeRun(status))
    snapshot = evolution.snapshot("worker")
    assert snapshot["outcome_history"] == [S, S, F, F, F]
    assert snapshot["consecutive_failures"] == 3
    assert snapshot["recent_outcomes"] == [F, F, F]
    assert snapshot["runs"] == 5
    assert snapshot["succeeded"] == 2
    assert snapshot["failed"] == 3


def test_ledger_history_bounded():
    evolution = AgentEvolution("s1")
    definition = FakeDefinition("worker")
    for _ in range(50):
        evolution.record(definition, FakeRun(S))
    snapshot = evolution.snapshot("worker")
    assert len(snapshot["outcome_history"]) == 20
    assert snapshot["consecutive_failures"] == 0
