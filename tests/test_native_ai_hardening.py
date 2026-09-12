"""Regression tests for the A81 hardening pass.

Each test pins one concrete defect the audit found (and its fix), so the
behavior can never silently drift back:

* rollback/verification blind spot for debug-loop repair files,
* a failed ReviewGate escaping the final-status decision,
* hash-seed-dependent planner grounding order,
* id collisions across processes (memory entries, run records),
* fixed-name temp files racing (state snapshot, training manifests),
* unverified model-version promotion,
* unpruned compile scanning, inconsistent skipped gates,
* missing cumulative proposal budget,
* SAFE-mode denials misreported as "needs approval".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from helpers_native_ai import (
    CALC_BROKEN,
    CALC_GOOD,
    ScriptedProvider,
    changes_text,
    scripted_fabric,
    write_repo,
)

from forge.native.engine import NativeAIEngine
from forge.native.verification import NativeVerifier
from forge.security.permissions import OperationMode


# -- engine bookkeeping ----------------------------------------------------

def test_debug_repairs_join_the_rollback_set(tmp_path):
    """A repair that the debug loop applies must be rolled back on failure.

    Before the fix, ``NativeDebugLoop`` repaired files outside the engine's
    ``applied_files`` ledger: the rollback set skipped them and the
    verification scan never saw them.
    """
    write_repo(tmp_path, calc=CALC_BROKEN,
               extra={"extra.py": "# keep me\n"})
    calc_before = (tmp_path / "calc.py").read_text(encoding="utf-8")

    def responder(prompt):
        if "Current failure" in prompt:  # the debug loop's repair request
            return changes_text({"extra.py": "print('repaired')\n"})
        return changes_text({"calc.py": "def add(a, b):\n    return a * b\n"})

    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(responder),
                            persist_status=False, max_debug_retries=1)
    result = engine.run("fix the broken add function in calc.py",
                        approved=True)
    assert result.final_status == "FAILED"
    report = result.report
    # The repair really happened (so this would leak without the fix)...
    assert report.debug["changed_files"] == ["extra.py"]
    # ...and was fully undone, together with the failing edit.
    assert report.rollback is True
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == calc_before
    assert (tmp_path / "extra.py").read_text(encoding="utf-8") == "# keep me\n"
    assert sorted(report.files_changed) == []
    # The verification pass saw the union of edit + repair files:
    security = [g for g in report.verification["gates"]
                if g["name"] == "security"]
    assert security and security[0]["executed"] is True


def test_failed_review_rolls_back_and_fails_the_run(tmp_path):
    """ReviewGate verdicts gate the run; tests passing is not enough.

    The scripted model "passes" the test suite but leaves a conflict marker
    in a new file — the deterministic severity gate must block, the changes
    must be rolled back, and no success strategy may be memorized.
    """
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text({
        "calc.py": CALC_GOOD,
        "notes.md": "<<<<<<< HEAD\ntodo: finish merge\n",
    }))
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(provider),
                            persist_status=False)
    result = engine.run("fix the broken add function in calc.py",
                        approved=True)
    assert result.final_status == "FAILED"
    report = result.report
    assert report.review["passed"] is False
    assert report.review["verdict"] == "BLOCK"
    assert report.review["gate"] == "forge.security.review.ReviewGate"
    # rollback covers both the edit and the polluted new file:
    assert report.rollback is True
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_BROKEN
    assert not (tmp_path / "notes.md").exists()
    # honest memory: a blocked run must not be recorded as a success strategy
    categories = [w["category"] for w in report.memory["written"]]
    assert "strategies" not in categories
    assert "failures" in categories


def test_safe_mode_denial_is_a_failure_not_a_pending_approval(tmp_path):
    """SAFE mode DENIES writes outright; that is refusal, not 'needs approval'.

    Only REQUIRE_APPROVAL maps to BLOCKED_APPROVAL (exit 2); a policy DENY
    must surface as FAILED with nothing written.
    """
    write_repo(tmp_path, calc=CALC_BROKEN)
    provider = ScriptedProvider(lambda prompt: changes_text(
        {"calc.py": CALC_GOOD}))
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(provider),
                            mode=OperationMode.SAFE.value,
                            persist_status=False)
    result = engine.run("fix the broken add function in calc.py",
                        approved=True)  # a pre-granted approval...
    assert result.final_status == "FAILED"
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == CALC_BROKEN
    names = [e["name"] for e in result.report.events]
    assert "change_blocked" not in names  # no bogus pending-approval state
    assert "apply_failed" in names
    # verification never ran (the run failed before it) -- nothing was
    # reported as checked that wasn't:
    assert not result.report.verification


# -- planner determinism -----------------------------------------------------

def _plan_json(repo_root: Path, fixture: Path, seed: str) -> str:
    code = (
        "import sys\n"
        "from unittest.mock import patch\n"
        "sys.argv = ['forge', 'native-ai', 'plan',\n"
        "            'explain subtract and helper in util.py',\n"
        "            '--root', %r, '--json']\n"
        "from forge.cli import main\n"
        "try:\n"
        "    main()\n"
        "except SystemExit as exc:\n"
        "    if exc.code not in (0, None):\n"
        "        raise\n" % (str(fixture),)
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)
    env["PYTHONHASHSEED"] = seed
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=120,
                          check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_plan_grounding_is_identical_across_hash_seeds(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    write_repo(tmp_path, extra={"util.py": "def helper():\n    return 2\n"
                                         "\n\ndef subtract(a, b):\n"
                                         "    return a - b\n"})
    outputs = [_plan_json(repo_root, tmp_path, seed)
               for seed in ("0", "1", "7", "42")]
    assert all(out == outputs[0] for out in outputs)
    plan = json.loads(outputs[0])["plan"]
    # Task-word order, not set iteration order, defines the matches:
    assert plan["matched_symbols"] == ["subtract", "helper"]


# -- identity, collisions, atomic writes --------------------------------------

def test_run_ids_never_collide():
    from forge.native.reporting import new_run_id
    ids = [new_run_id() for _ in range(500)]
    assert len(set(ids)) == 500


def test_memory_ids_unique_across_instances(tmp_path):
    from forge.native.memory import NativeMemory
    m1 = NativeMemory(tmp_path)
    m2 = NativeMemory(tmp_path)  # second engine process in the same second
    r1 = m1.record_decision("task one", "s1")
    r2 = m2.record_decision("task two", "s2")
    assert r1.id != r2.id
    files = sorted((tmp_path / ".forge" / "memory" / "native").
                   glob("**/*.json"))
    assert len(files) == 2  # neither entry overwrote the other


def test_snapshot_writers_do_not_race_on_temp_files(tmp_path):
    from forge.native.state import StateTracker, read_snapshot
    trackers = [StateTracker(tmp_path, persist=True) for _ in range(4)]
    errors = []

    def hammer(index):
        tracker = trackers[index % len(trackers)]
        for cycle in range(30):
            try:
                tracker.set_retry(cycle, 100, active=True,
                                  last_reason="stress %d-%d" % (index, cycle))
            except Exception as exc:  # pragma: no cover - would be a bug
                errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    snapshot = read_snapshot(tmp_path)
    assert snapshot is not None  # parsed cleanly: never torn
    assert not list((tmp_path / ".forge" / "native").glob("*.tmp"))


# -- training store ------------------------------------------------------------

def test_promotion_and_active_state_recheck_the_artifact(tmp_path):
    from forge.native.training import ModelVersionStore
    store = ModelVersionStore(tmp_path)
    artifact = tmp_path / "models" / "tiny.gguf"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"real-bytes")
    store.register("v1", artifact, evaluation={"passed": True})
    assert store.get("v1")["artifact_missing"] is False
    store.promote("v1")
    assert store.active()["artifact_missing"] is False

    artifact.unlink()  # the file disappears after registration
    assert store.get("v1")["artifact_missing"] is True
    assert store.list()[0]["artifact_missing"] is True
    active = store.active()
    assert active["artifact_missing"] is True  # surfaced, never hidden
    with pytest.raises(ValueError, match="disappeared"):
        store.promote("v1")
    # manifest writes are atomic: no temp litter is ever left behind
    assert not list((tmp_path / ".forge" / "native" / "training" /
                     "versions").glob("*.tmp"))


# -- verification gates --------------------------------------------------------

def test_skipped_build_and_lint_gates_are_recorded(tmp_path):
    write_repo(tmp_path)
    report = NativeVerifier(tmp_path).verify(
        changed_files=["calc.py"], diff_text="x",
        run_tests=False, run_build=False, run_lint=False)
    gates = {gate.name: gate for gate in report.gates}
    for name in ("tests", "build", "lint"):
        assert name in gates, "scoped-off gates must be recorded, not hidden"
        assert gates[name].executed is False
        assert "scoped off" in gates[name].details
    assert report.status_dict()["status"] == "PARTIAL"


def test_compile_scan_prunes_vendored_trees(tmp_path):
    # Broken Python under excluded trees must not poison (or slow) the scan;
    # broken Python under real source must still fail it.
    write_repo(tmp_path, extra={"node_modules/pkg/bad.py":
                                "def broken(:\n",
                                ".git/hooks/x.py": "import (\n",
                                "pkg/ok.py": "VALUE = 1\n"})
    gate = NativeVerifier(tmp_path).compile_check(None)
    assert gate.passed, gate.details
    Path(tmp_path, "app", "bad.py").parent.mkdir(exist_ok=True)
    Path(tmp_path, "app", "bad.py").write_text("def also_broken(:\n",
                                               encoding="utf-8")
    failing = NativeVerifier(tmp_path).compile_check(None)
    assert not failing.passed


# -- coding engine budget -------------------------------------------------------

def test_proposal_cumulative_byte_budget(tmp_path):
    payload = {}
    for index in range(30):
        payload["gen/mod_%02d.py" % index] = ("X = %d\n" % index) + \
            ("c" * 200000)  # ~6 MB total: over the 4 MB cap
    provider = ScriptedProvider(
        lambda prompt: json.dumps({"explanation": "many files",
                                   "changes": payload}))
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric(provider),
                            persist_status=False)
    # raise the model-output ceiling so the hub hands the whole oversized
    # payload to the budget check itself (that is what is under test)
    engine.coding.max_output_chars = 10 * 1024 * 1024
    proposal = engine.coding.propose_changes("regenerate modules", "")
    assert proposal.ok
    total = sum(len(c.encode("utf-8")) for c in proposal.changes.values())
    assert 0 < len(proposal.changes) < 30
    assert total <= engine.coding.MAX_TOTAL_BYTES


# -- CLI: plan + history ---------------------------------------------------------

def _run_cli(monkeypatch, argv, capsys):
    from forge.cli import main
    monkeypatch.setattr(sys, "argv", ["forge"] + argv)
    try:
        main()
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr().out


def test_cli_plan_shows_grounding_without_executing(tmp_path, capsys,
                                                    monkeypatch):
    write_repo(tmp_path, extra={"util.py": "def helper():\n    return 2\n"})
    code, out = _run_cli(monkeypatch, ["native-ai", "plan",
                                       "explain helper in util.py",
                                       "--root", str(tmp_path)], capsys)
    assert code == 0
    assert "Native AI plan" in out
    assert "1 file match" in out and "1 symbol match" in out
    assert "util.py" in out
    # nothing executed: the plan is advice, not a run
    assert "nothing was executed" in out
    assert not (tmp_path / ".forge" / "native" / "runs").exists() or \
        not list((tmp_path / ".forge" / "native" / "runs").glob("*.json"))


def test_cli_history_lists_and_reads_run_records(tmp_path, capsys,
                                                  monkeypatch):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=True)
    result = engine.run("analyze the calc module and describe its tests")
    run_id = result.report.run_id
    code, out = _run_cli(monkeypatch, ["native-ai", "history",
                                       "--root", str(tmp_path)], capsys)
    assert code == 0
    assert run_id in out and "newest first" in out
    code, out = _run_cli(monkeypatch, ["native-ai", "history",
                                       "--run", run_id, "--root",
                                       str(tmp_path)], capsys)
    assert code == 0
    record = json.loads(out)
    assert record["run_id"] == run_id
    assert record["final_status"] == "COMPLETED"
    code, _out = _run_cli(monkeypatch, ["native-ai", "history",
                                        "--run", "nope", "--root",
                                        str(tmp_path)], capsys)
    assert code == 1
