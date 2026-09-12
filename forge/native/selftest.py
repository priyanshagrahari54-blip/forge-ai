"""Deterministic, self-contained self-test for the Native AI Engine.

Powers ``forge native-ai test``: every layer of the engine is exercised
against a freshly created temporary fixture — startup, planning,
repository inspection, context generation, reasoning-backend selection,
tool execution, policy enforcement, verification, failure classification,
memory, and no-model operation. The fixture is written by the test itself,
the expectations are fixed numbers and names, and nothing touches the user's
repository, network, or any model endpoint.

Honesty rules mirrored from the engine: the self-test never asserts that a
model exists; the no-model checks *require* the refusal. A check only passes
on measured results (real pytest invocations, real file contents).
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from forge.native.capabilities import capability_matrix
from forge.native.engine import NativeAIEngine
from forge.native.memory import SecretInMemoryError
from forge.native.state import EngineState, read_snapshot
from forge.native.verification import NativeVerifier


def _fixture_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="forge-native-selftest-"))
    (root / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "import calc\n\n\ndef test_add():\n    assert calc.add(2, 2) == 4\n",
        encoding="utf-8")
    return root


def run_native_selftest() -> Dict[str, Any]:
    """Run every check; return a structured, JSON-safe report."""
    checks: List[Dict[str, Any]] = []
    root: Any = None

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            checks.append({"name": name, "passed": True,
                           "detail": str(detail or "ok")})
        except Exception as exc:
            checks.append({"name": name, "passed": False,
                           "detail": "%s: %s" % (type(exc).__name__, exc)})

    try:
        root = _fixture_root()

        # 1. startup: engine constructs, status surface complete.
        engine = NativeAIEngine(root=root, project="selftest",
                                persist_status=True)

        def startup() -> str:
            status = engine.status()
            assert status["engine"] == "forge-native-ai"
            assert status["state"]["engine_state"] == EngineState.IDLE.value
            assert len(status["capabilities"]) == len(capability_matrix())
            return "engine ready, %d capabilities labeled" % len(
                status["capabilities"])
        check("startup", startup)

        # 2. repository inspection through RepositoryIntelligence.
        def inspection() -> str:
            intelligence = engine._ensure_intelligence()
            names = {s.name for s in intelligence.symbols.by_file("calc.py")}
            assert "add" in names, names
            tests = intelligence.tests.tests_for_source("calc.py") \
                if hasattr(intelligence.tests, "tests_for_source") else []
            return "symbols=%s mapped_tests=%s" % (sorted(names), tests)
        check("repository-inspection", inspection)

        # 3. planning: an edit-class task yields the seven-step shape.
        def planning() -> str:
            plan = engine.run_probe_plan("fix calc.py add to handle strings")
            assert plan["ok"] and plan["has_edit_step"]
            from forge.native.planner import NativePlanner
            steps = NativePlanner(engine._ensure_intelligence()).plan(
                "fix calc.py add to handle strings").steps
            kinds = [s.kind.value for s in steps]
            assert kinds == ["inspect", "reason", "edit", "test", "debug",
                             "review", "finish"], kinds
            return "step kinds ordered: " + " -> ".join(kinds)
        check("planning", planning)

        # 4. context: budgeted, fingerprinted, sections labeled.
        def context_build() -> str:
            context = engine.context_engine.build(
                "fix calc.py add", None, engine._ensure_intelligence(),
                engine.memory, git_root=root)
            again = engine.context_engine.build(
                "fix calc.py add", None, engine._ensure_intelligence(),
                engine.memory, git_root=root)
            assert context.fingerprint == again.fingerprint, \
                "fingerprint must be stable"
            assert context.estimated_tokens <= engine.context_engine.max_tokens
            names = {s.name for s in context.sections}
            assert {"task", "relevant_files", "recent_changes",
                    "previous_failures"} <= names, names
            return "fingerprint=%s tokens=%d" % (context.fingerprint,
                                                  context.estimated_tokens)
        check("context-generation", context_build)

        # 5. reasoning backend selection without a model.
        def backend_selection() -> str:
            hub = engine.hub
            assert hub.status()["generative_ready"] is False
            from forge.native.reasoning import (
                ReasoningKind,
                ReasoningRequest,
                RefusalCode,
            )
            refusal = hub.respond(ReasoningRequest(
                kind=ReasoningKind.REPAIR, task="fix it",
                payload={"failure_output": "AssertionError: 1 == 2"}))
            assert not refusal.ok
            assert refusal.refusal == RefusalCode.NEURAL_REQUIRED.value
            diagnose = hub.respond(ReasoningRequest(
                kind=ReasoningKind.DIAGNOSE, task="t",
                payload={"failure_output":
                         'File "calc.py", line 1\nSyntaxError: bad'}))
            assert diagnose.ok and \
                diagnose.data["category"] == "syntax", diagnose.data
            return "deterministic active; REPAIR refused with " \
                   "NEURAL_REQUIRED; diagnose=categorizes"
        check("reasoning-backends", backend_selection)

        # 6. verification: real compile + test gates on the fixture.
        def verification() -> str:
            verifier = NativeVerifier(root)
            report = verifier.verify(changed_files=["calc.py",
                                                    "test_calc.py"],
                                     diff_text="def add(a, b):\n"
                                               "    return a + b\n",
                                     run_build=False, run_lint=False)
            assert report.all_passed, json.dumps(report.to_dict(),
                                                 indent=1)[:600]
            # break syntax -> compile must fail (a failure stays a failure)
            broken = root / "broken_tmp.py"
            broken.write_text("def oops(:\n", encoding="utf-8")
            try:
                second = verifier.compile_check(["broken_tmp.py"])
                assert not second.passed, second.details
            finally:
                broken.unlink()
            return "compile+tests+security+diff pass; syntax break caught"
        check("verification", verification)

        # 7. policy enforcement: SAFE mode cannot write, ever.
        def policy() -> str:
            safe_engine = NativeAIEngine(root=root, mode="safe",
                                         persist_status=False)
            outcome = safe_engine.coding.apply_changes(
                {"sneaky.py": "print('no')\n"}, approved=True)
            assert not outcome.ok, "SAFE mode must deny writes even " \
                                   "with approved=True"
            assert not (root / "sneaky.py").exists()
            probe = safe_engine.coding.inspect_file("calc.py")
            assert probe.ok and "def add" in probe.content
            return "SAFE mode: write denied (approval never overrides " \
                   "mode), reads allowed"
        check("policy-enforcement", policy)

        # 8. memory: roundtrip, redaction-before-store, and hard rejection.
        def memory() -> str:
            record = engine.memory.record_decision("selftest task",
                                                    "checked decision",
                                                    source="selftest")
            recalled = [r for r in engine.memory.decisions(limit=5)
                        if r.id == record.id]
            assert recalled, "decision must be recallable"
            # recoverable secret: stored only as the redacted form, and the
            # on-disk bytes must not contain the key material.
            pem = engine.memory.record(
                "decisions", "secret probe", "selftest",
                {"note": "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n"})
            assert pem.redacted, "PEM content must be flagged redacted"
            on_disk = engine.memory.store.load("%s/%s.json"
                                               % (pem.category, pem.id))
            assert on_disk and "MIIabc" not in on_disk, \
                "raw key material must never reach disk"
            # unrecoverable secret-shaped content: refuse the write.
            try:
                engine.memory.record("decisions", "secret probe 2",
                                     "selftest",
                                     {"note": 'aws_secret_access_key = '
                                              '"AKIA1234567890ABCDEF"'})
                raise AssertionError("unredactable credential content must "
                                     "be refused")
            except SecretInMemoryError:
                pass
            return "roundtrip ok; secrets redacted or refused on write"
        check("memory", memory)

        # 9. no-model operation: full run refuses honestly, writes nothing.
        def no_model_run() -> str:
            calc_before = (root / "calc.py").read_text(encoding="utf-8")
            result = NativeAIEngine(root=root, project="selftest",
                                    persist_status=True,
                                    memory_enabled=True).run(
                "implement multiply in calc.py with tests")
            assert result.final_status == "NEEDS_MODEL", \
                "edit-class plan without a model must be NEEDS_MODEL, not " \
                "a fabricated success"
            assert (root / "calc.py").read_text(
                encoding="utf-8") == calc_before
            assert "code_generation" in result.report.skipped_neural
            snapshot = read_snapshot(root) or {}
            assert snapshot.get("engine_state") == "needs_model", snapshot
            assert snapshot.get("stage", {}).get("kind") in (
                "finish", "review"), snapshot.get("stage")
            assert snapshot.get("reasoning_backend", {}).get(
                "name") == "native-deterministic"
            return "NEEDS_MODEL reported with refused steps; no writes; " \
                   "snapshot updated"
        check("no-model-operation", no_model_run)

        # 10. tests actually run: real pytest inside a test-only run.
        def tests_run() -> str:
            result = NativeAIEngine(root=root, project="selftest",
                                    persist_status=False).run(
                "run the tests for this project")
            ran = [r for r in result.report.test_runs if r.get("executed")]
            assert ran and ran[-1]["passed"], result.report.test_runs
            assert result.final_status == "COMPLETED"
            return "real pytest executed via the constrained run_tests tool "\
                "(exit %s)" % ran[-1]["exit_code"]
        check("test-execution", tests_run)

    except Exception as exc:  # fixture setup failed wholesale
        checks.append({"name": "fixture", "passed": False,
                       "detail": "setup failed: %s" % exc})
    finally:
        if root is not None:
            shutil.rmtree(str(root), ignore_errors=True)

    passed_count = sum(1 for c in checks if c["passed"])
    return {
        "passed": bool(checks) and passed_count == len(checks),
        "total": len(checks),
        "passed_count": passed_count,
        "checks": checks,
        "error": "",
    }
