from __future__ import annotations

from pathlib import Path

import pytest

from forge.orchestration.auto_milestones import (
    AutoMilestoneRunner,
    MilestoneSpec,
    MilestoneStatus,
)


def test_dependency_ready_milestones_continue_automatically(tmp_path: Path) -> None:
    runner = AutoMilestoneRunner(
        [
            MilestoneSpec("m1", "foundation"),
            MilestoneSpec("m2", "fabric", depends_on=("m1",)),
            MilestoneSpec("m3", "verification", depends_on=("m2",)),
        ],
        state_path=tmp_path / "state.json",
    )
    seen: list[str] = []

    result = runner.run(lambda spec, state: seen.append(spec.id) or {"attempt": state.attempts})

    assert result.success
    assert seen == ["m1", "m2", "m3"]
    assert runner.snapshot()["passed"] == 3


def test_failed_milestone_retries_and_then_unblocks_dependants(tmp_path: Path) -> None:
    runner = AutoMilestoneRunner(
        [
            MilestoneSpec("m1", "unstable", max_attempts=2),
            MilestoneSpec("m2", "dependent", depends_on=("m1",)),
        ],
        state_path=tmp_path / "state.json",
    )
    attempts = {"m1": 0}

    def execute(spec, _state):
        if spec.id == "m1":
            attempts["m1"] += 1
            if attempts["m1"] == 1:
                raise RuntimeError("transient build failure")
        return {"ok": True}

    result = runner.run(execute)

    assert result.success
    assert attempts["m1"] == 2
    assert runner.states["m1"].status == MilestoneStatus.PASSED.value
    assert runner.states["m2"].status == MilestoneStatus.PASSED.value


def test_restart_resumes_persisted_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    first = AutoMilestoneRunner([MilestoneSpec("m1", "one")], state_path=path)
    first.run(lambda spec, state: {"checkpoint": state.attempts})

    second = AutoMilestoneRunner([MilestoneSpec("m1", "one")], state_path=path)
    assert second.states["m1"].status == MilestoneStatus.PASSED.value
    assert second.ready() == []


def test_cycle_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cycle"):
        AutoMilestoneRunner(
            [
                MilestoneSpec("a", "A", depends_on=("b",)),
                MilestoneSpec("b", "B", depends_on=("a",)),
            ],
            state_path=tmp_path / "state.json",
        )
