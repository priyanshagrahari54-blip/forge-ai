"""Native planner: deterministic, repository-grounded plan structures."""
from __future__ import annotations

import pytest

from helpers_native_ai import write_repo

from forge.intelligence.repository import RepositoryIntelligence
from forge.native.planner import NativePlan, NativePlanner, StepKind, \
    TaskClass
from forge.native.state import StageKind


@pytest.fixture()
def intelligence(tmp_path):
    write_repo(tmp_path, extra={"pkg/util.py":
                                "def helper(x):\n    return x * 2\n",
                                "pkg/test_util.py":
                                "def test_helper():\n    assert True\n"})
    return RepositoryIntelligence.build(tmp_path)


def test_step_kinds_are_the_seven_canonical_stages():
    assert list(StageKind.ordered()) == [
        StepKind.INSPECT, StepKind.REASON, StepKind.EDIT, StepKind.TEST,
        StepKind.DEBUG, StepKind.REVIEW, StepKind.FINISH]
    # the planner reuses the stage axis instead of a parallel vocabulary
    assert StepKind is StageKind


def test_empty_task_is_rejected(tmp_path):
    planner = NativePlanner(None)
    with pytest.raises(ValueError):
        planner.plan("   ")


def test_fix_task_produces_full_edit_shape(intelligence):
    plan = NativePlanner(intelligence).plan(
        "fix the add function in calc.py to handle strings")
    kinds = [step.kind for step in plan.steps]
    assert kinds == [StepKind.INSPECT, StepKind.REASON, StepKind.EDIT,
                     StepKind.TEST, StepKind.DEBUG, StepKind.REVIEW,
                     StepKind.FINISH]
    assert plan.task_class == TaskClass.FIX
    assert plan.validate() == []


def test_analyze_task_has_no_edit_or_debug_steps(intelligence):
    plan = NativePlanner(intelligence).plan(
        "analyze the repository and describe the calc module")
    kinds = [step.kind for step in plan.steps]
    assert StepKind.EDIT not in kinds and StepKind.DEBUG not in kinds
    assert plan.task_class == TaskClass.ANALYZE
    assert not plan.requires_neural()


def test_edit_steps_are_marked_neural_requiring(intelligence):
    plan = NativePlanner(intelligence).plan("implement multiply in calc.py")
    edit = plan.step(StepKind.EDIT)
    assert edit is not None and edit.requires_neural
    test_step = plan.step(StepKind.TEST)
    assert test_step is not None and not test_step.requires_neural


def test_targets_are_grounded_in_real_files(intelligence):
    plan = NativePlanner(intelligence).plan(
        "fix pkg/util.py helper behavior")
    inspect_step = plan.step(StepKind.INSPECT)
    assert "pkg/util.py" in inspect_step.target_files
    assert plan.matched_files == ("pkg/util.py",)


def test_unresolved_path_like_tokens_are_reported_not_dropped(intelligence):
    plan = NativePlanner(intelligence).plan(
        "fix ghost/missing_module.py to work")
    assert "ghost/missing_module.py" in plan.unresolved_references


def test_ambiguous_task_defaults_low_confidence_implement(intelligence):
    plan = NativePlanner(intelligence).plan(
        "something something widgets in the void")
    assert plan.confidence == "low"
    assert plan.task_class == TaskClass.IMPLEMENT
    # classification stays honest about its own method
    classification = NativePlanner(intelligence).classify(
        "something something widgets in the void")
    assert "neural" in classification.method


def test_mixed_read_and_write_verbs_prefer_safe_analyze(intelligence):
    plan = NativePlanner(intelligence).plan(
        "analyze the calc module and add logging to it")
    assert plan.task_class == TaskClass.ANALYZE
    assert any("safer analyze plan" in note for note in plan.notes)


def test_test_targets_come_from_the_existing_test_mapping(intelligence):
    plan = NativePlanner(intelligence).plan("fix calc.py add")
    test_step = plan.step(StepKind.TEST)
    assert "test_calc.py" in test_step.test_targets


def test_plan_dependencies_are_valid_and_acyclic(intelligence):
    plan = NativePlanner(intelligence).plan("refactor calc.py")
    assert plan.validate() == []
    for i, step in enumerate(plan.steps):
        if i == 0:
            assert step.depends_on == ()
        else:
            assert step.depends_on == (plan.steps[i - 1].id,)


def test_invalid_plan_is_rejected_by_the_planner():
    plan = NativePlan(task_text="x", task_class="fix", confidence="low",
                     steps=[])
    assert plan.validate() == []  # empty plans validate vacuously
    from forge.native.planner import NativeStep
    a = NativeStep(id="a", kind=StepKind.EDIT, description="")
    b = NativeStep(id="b", kind=StepKind.TEST, description="",
                   depends_on=("a",))
    a.depends_on = ("b",)
    plan.steps = [a, b]
    problems = plan.validate()
    assert any("cycle" in problem for problem in problems)


def test_plan_serialization_round_trips(intelligence):
    plan = NativePlanner(intelligence).plan("implement multiply in calc.py")
    data = plan.to_dict()
    assert data["task_class"] == plan.task_class
    assert len(data["steps"]) == len(plan.steps)
    assert data["steps"][2]["kind"] == "edit"
    assert "requires_neural" in data["steps"][2]
