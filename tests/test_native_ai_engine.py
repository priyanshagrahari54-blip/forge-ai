"""End-to-end Native AI Engine runs: no-model, with-model, failures.

The central honesty claims of A81 are verified here on complete pipelines:

* a code task without any neural model ends in ``NEEDS_MODEL`` — the plan
  ran, the tests ran, nothing was written, and nothing was claimed done;
* an analysis-only task completes free of charge, with a real pytest run
  recorded;
* with a scripted real model, a full edit→test→debug→verify cycle lands a
  genuine change (authorized) and reports COMPLETED;
* an applied-but-wrong change is rolled back exactly and the run FAILS —
  never optimistically PARTIAL;
* policy DENY stops the run as BLOCKED_APPROVAL with no file writes;
* run records persist under ``.forge/native/runs`` for later dataset use.
"""
from __future__ import annotations

import json

from helpers_native_ai import (
    CALC_BROKEN,
    CALC_GOOD,
    ScriptedProvider,
    changes_text,
    scripted_fabric,
    write_repo,
)

from forge.native.engine import NativeAIEngine, list_run_records
from forge.native.state import EngineState, TaskState, read_snapshot


def test_no_model_code_task_is_needs_model_not_fake_success(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    engine = NativeAIEngine(tmp_path, persist_status=True)
    result = engine.run("fix the add function in calc.py")
    assert result.final_status == "NEEDS_MODEL"
    assert result.needs_model and not result.ok
    report = result.report
    # real analysis and tests happened:
    assert report.plan["task_class"] == "fix"
    assert report.test_runs and report.test_runs[0]["executed"]
    assert report.test_runs[0]["passed"] is False  # the bug is still there
    # no writes, no fabricated code:
    assert report.files_changed == []
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_BROKEN
    assert "code_generation" in report.skipped_neural
    refusal = report.plan["edit_refusal"]
    assert refusal["refusal"] == "NEURAL_REQUIRED"
    assert "no code was" in refusal["honest_note"] or \
        "nothing was written" in refusal["honest_note"]
    # snapshot reflects the honest state:
    snapshot = read_snapshot(tmp_path)
    assert snapshot["engine_state"] == EngineState.NEEDS_MODEL.value
    assert snapshot["task"]["state"] == TaskState.NEEDS_MODEL.value
    # memory recorded the situation (decision + verification):
    written = [w["category"] for w in report.memory["written"]]
    assert "decisions" in written and "verification" in written


def test_analysis_task_completes_without_any_model(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False)
    result = engine.run("analyze the calc module and describe its tests")
    assert result.final_status == "COMPLETED"
    report = result.report
    assert report.task_class == "analyze"
    kinds = [step["kind"] for step in report.plan["steps"]]
    assert "edit" not in kinds
    # events are recorded in order and stage history exists:
    names = [event["name"] for event in report.events]
    assert "planned" in names and "context_built" in names
    assert "verifying" in report.stages


def test_model_driven_edit_lands_and_verifies(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": CALC_GOOD}))
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(provider),
                            persist_status=False)
    result = engine.run("fix the broken add function in calc.py",
                        approved=True)
    report = result.report
    assert result.final_status == "COMPLETED", report.error
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_GOOD
    assert report.files_changed == ["calc.py"]
    assert report.test_runs[-1]["passed"] is True
    edit = [s for s in report.plan["steps"] if s["kind"] == "edit"][0]
    assert edit["status"] == "done"
    assert edit["result"]["model"]["model"] == "scripted-model"
    assert report.verification["status"] in ("PASS", "PARTIAL")


def test_applied_but_broken_change_is_rolled_back(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    before = (tmp_path / "calc.py").read_text(encoding="utf-8")
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": "def add(a, b):\n    return a * b\n"}))
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(provider),
                            persist_status=False, max_debug_retries=1)
    result = engine.run("fix the broken add function in calc.py",
                        approved=True)
    assert result.final_status == "FAILED"
    assert result.report.rollback is True
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == before
    assert result.report.files_changed == []  # nothing survived to claim
    assert result.report.verification["status"] == "FAIL"
    from forge.native.state import read_snapshot as _read
    assert _read(tmp_path) is None


def test_policy_denial_blocks_without_writing(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": CALC_GOOD}))
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(provider),
                            mode="assisted", persist_status=False)
    result = engine.run("fix the broken add function in calc.py",
                        approved=False)  # nobody approves in assisted mode
    assert result.final_status == "BLOCKED_APPROVAL"
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_BROKEN
    assert "blocked" in result.report.error.lower()
    # policy decisions were recorded as events (audit trail, no bypass):
    decisions = [e for e in result.report.events
                 if e["name"] == "permission_decision"]
    assert decisions


def test_pre_cancelled_engine_aborts_at_first_boundary(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False)
    engine.cancel()  # cancel before run: the first boundary check aborts
    result = engine.run("analyze the repository")
    assert result.final_status == "CANCELLED"
    assert result.report.error == "cancelled by operator"
    assert result.report.stages == [] or "understanding" not in \
        result.report.stages[1:]
    # sticky: cancelled until explicitly resumed
    assert engine.run("analyze again").final_status == "CANCELLED"
    engine.resume()
    assert engine.run("analyze again").final_status == "COMPLETED"


def test_cancel_mid_run_stops_before_later_stages(tmp_path):
    write_repo(tmp_path)
    seen = []

    def on_event(name, details):
        seen.append(name)
        if name == "stage" and details.get("stage") == "planning":
            engine.cancel()  # arrive at the next boundary already cancelled

    engine = NativeAIEngine(tmp_path, persist_status=False,
                            on_event=on_event)
    result = engine.run("analyze the repository")
    assert result.final_status == "CANCELLED"
    assert "verifying" not in result.report.stages  # stopped before the end


def test_run_records_are_persisted_and_listable(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False)
    result = engine.run("analyze the repository")
    records = list_run_records(tmp_path)
    assert records and records[0]["run_id"] == result.report.run_id
    record_path = tmp_path / ".forge" / "native" / "runs" / (
        result.report.run_id + ".json")
    data = json.loads(record_path.read_text(encoding="utf-8"))
    assert data["final_status"] == "COMPLETED"
    # redaction: run records never carry credential-shaped material
    assert "api_key = \"" not in json.dumps(data)


def test_memory_records_written_on_success(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False)
    engine.run("analyze the calc module")
    summary = engine.memory.summary()
    assert summary["entries"] >= 2  # decision + verification at minimum
    decisions = engine.memory.decisions(limit=3)
    assert decisions and "task_class=analyze" in decisions[0].payload[
        "decision"]


def test_engine_never_fabricates_completion_on_engine_errors(tmp_path):
    # An empty task is a caller error, not a run — fail fast, no snapshot
    # mutation to a fake state.
    engine = NativeAIEngine(tmp_path, persist_status=False)
    import pytest
    with pytest.raises(ValueError):
        engine.run("   ")
    assert engine.status()["state"]["engine_state"] == "idle"


def test_dataset_capture_flag_is_labeled_only(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False,
                            dataset_capture=True)
    result = engine.run("analyze the repository")
    assert result.report.dataset_captured is True
    # and the run record says so truthfully (no output was captured anyway
    # since there was no model output in an analyze run):
    data = json.loads((tmp_path / ".forge/native/runs" /
                       (result.report.run_id + ".json")).read_text(
        encoding="utf-8"))
    assert data["dataset_captured"] is True
