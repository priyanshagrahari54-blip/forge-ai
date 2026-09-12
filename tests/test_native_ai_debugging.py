"""Debug loop: classify → context → repair plan → authorized fix → retest.

With no model the loop must stop *honestly* (neural_required) after real
classification and context collection; with a scripted model that actually
returns a fixing change, it repairs, re-verifies, and stops at the first
green retest; and the retry bound is enforced exactly.
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

from forge.native.engine import NativeAIEngine
from forge.native.reasoning import (
    NativeDeterministicBackend,
    ReasoningKind,
    ReasoningRequest,
)


def loop_for(tmp_path, mode="assisted", responder=None, retries=3):
    fabric = scripted_fabric(responder) if responder is not None else None
    engine = NativeAIEngine(tmp_path, mode=mode, fabric=fabric,
                            max_debug_retries=retries,
                            persist_status=False)
    return engine, engine.debug_loop


def test_failure_classification_is_deterministic():
    backend = NativeDeterministicBackend()
    for output, expected in (
            ("AssertionError: assert 1 == 2", "assertion"),
            ("ImportError: cannot import name 'nope'", "import"),
            ("FAILED tests/test_x.py::test_y - TimeoutExpired", "timeout")):
        result = backend.respond(ReasoningRequest(
            kind=ReasoningKind.DIAGNOSE, task="t",
            payload={"failure_output": output}))
        assert result.data["category"] == expected, output


def test_no_model_loop_records_refusal_and_keeps_failure(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    engine, loop = loop_for(tmp_path)
    result = loop.run("fix calc.py add", "", test_paths=["test_calc.py"],
                      approved=True)
    assert not result.success
    assert result.stopped_reason == "neural_required"
    cycle = result.cycles[0]
    assert cycle.classification == "assertion"
    # context collection read real traceback frames through the runtime:
    assert cycle.failure_context_files
    assert all(".." not in p and not p.startswith("/")
               for p in cycle.failure_context_files)
    assert not cycle.repair.get("changes")  # nothing was fabricated
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_BROKEN


def test_retry_bound_of_zero_never_repairs(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    engine, loop = loop_for(tmp_path, responder=lambda prompt: changes_text(
        {"calc.py": CALC_GOOD}), retries=0)
    result = loop.run("fix calc.py", test_paths=["test_calc.py"],
                      approved=True)
    assert not result.success
    assert result.stopped_reason in ("retry_bound_reached", "tests_passed")
    assert result.cycles[0].reason == "retry budget is 0; no repair attempted"
    # and no repair was applied with zero budget:
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_BROKEN


def test_model_repair_fixes_and_stops_after_green_retest(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": CALC_GOOD}))
    engine, loop = loop_for(tmp_path, responder=provider)
    result = loop.run("fix calc.py add so it returns the sum",
                      test_paths=["test_calc.py"], approved=True)
    assert result.success
    assert result.stopped_reason == "tests_passed"
    assert len(result.cycles) == 1
    cycle = result.cycles[0]
    assert cycle.apply["ok"] and cycle.apply["files"] == ["calc.py"]
    assert cycle.retest["passed"] is True
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_GOOD
    assert len(provider.calls) == 1  # one repair ask, then green retest


def test_persistent_failure_burns_exactly_the_retry_budget(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": "def add(a, b):\n    return a * b\n"}))
    engine, loop = loop_for(tmp_path, responder=provider, retries=2)
    result = loop.run("fix calc.py", test_paths=["test_calc.py"],
                       approved=True)
    assert not result.success
    assert len(result.cycles) == 2
    assert result.stopped_reason == "retry_bound_reached"
    assert len(provider.calls) == 2  # bounded: never more than max_retries
    assert all(cycle.retest["passed"] is False for cycle in result.cycles)


def test_invalid_model_repairs_are_rejected_before_writing(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: json.dumps(
        {"explanation": "evil", "changes": {
            "test_calc.py": "def test_add():\n    assert True\n",
            ".forge/secret": "x"}}))
    engine, loop = loop_for(tmp_path, responder=provider, retries=1)
    result = loop.run("fix calc.py", test_paths=["test_calc.py"],
                      approved=True)
    assert not result.success
    assert result.stopped_reason == "invalid_proposal"
    assert "ChangeSet" in result.cycles[0].reason
    # the weakening edit and the .forge write both never happened (the
    # ChangeSet engine rejects the whole transaction atomically):
    assert "assert True" not in (tmp_path / "test_calc.py").read_text(
        encoding="utf-8")
    assert not (tmp_path / ".forge" / "secret").exists()


def test_blocked_repair_surfaces_policy_not_silence(tmp_path):
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": CALC_GOOD}))
    engine, loop = loop_for(tmp_path, mode="assisted", responder=provider)
    result = loop.run("fix calc.py", test_paths=["test_calc.py"],
                      approved=False)  # no approval -> gate blocks the write
    assert not result.success
    assert result.stopped_reason in ("policy_blocked", "neural_required",
                                     "backend_error")
    cycle = result.cycles[0]
    assert not cycle.apply.get("ok", True) or cycle.apply == {}
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_BROKEN
