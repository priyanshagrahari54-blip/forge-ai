"""Controlled self-improvement (A81): analysis, proposals, isolated
candidates, mandatory acceptance gates, ledger, rollback, bounds, and the
hard "never" guardrails."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from forge.security.permissions import OperationMode  # noqa: E402
from forge.self_improvement import (  # noqa: E402
    HARD_MAX_ITERATIONS,
    Candidate,
    CandidateRunner,
    EvidenceCollector,
    GuardrailViolation,
    Guardrails,
    ImprovementLedger,
    ImprovementProposal,
    PolicyApproval,
    ProposalGenerator,
    ProtectedFileAuthorization,
    RollbackManager,
    SelfAnalyzer,
    SelfImprovementAcceptance,
    SelfImprovementEngine,
    validate_proposal,
)
from forge.self_improvement.evidence import parse_pytest_output  # noqa: E402


# -- fixtures -------------------------------------------------------------------

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def make_repo(root: Path, *, broken: bool = True) -> Path:
    (root / "forge").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "forge" / "__init__.py").write_text("", encoding="utf-8")
    (root / "forge" / "mathy.py").write_text(BROKEN if broken else FIXED, encoding="utf-8")
    (root / "forge" / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_mathy.py").write_text(
        "from forge.mathy import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    (root / "tests" / "test_other.py").write_text(
        "from forge.other import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8")
    return root


def fixing_producer(_root: Path, _proposal: ImprovementProposal) -> dict[str, str]:
    return {"forge/mathy.py": FIXED}


def breaking_producer(_root: Path, _proposal: ImprovementProposal) -> dict[str, str]:
    # "Fixes" mathy but breaks the other module's test.
    return {"forge/mathy.py": FIXED, "forge/other.py": "VALUE = 2\n"}


def noop_producer(_root: Path, _proposal: ImprovementProposal) -> dict[str, str]:
    return {"forge/mathy.py": BROKEN + "# no-op comment\n"}


def make_engine(root: Path, producer, **kwargs) -> SelfImprovementEngine:
    analyzer = SelfAnalyzer(root)
    return SelfImprovementEngine(root, producer=producer, analyzer=analyzer, **kwargs)


COLLECT = {"run_tests": True, "measure_host": False}


# -- 1. self-analysis ------------------------------------------------------------

def test_pytest_output_parsing():
    out = ("FAILED tests/test_x.py::test_a - AssertionError\n"
           "ERROR tests/test_y.py::test_b\n"
           "1 failed, 12 passed, 1 error in 0.5s\n")
    parsed = parse_pytest_output(out)
    assert parsed == {"passed": 12, "failed": 1, "errors": 1, "skipped": 0,
                      "summary_found": True,
                      "failing_tests": ["tests/test_x.py::test_a", "tests/test_y.py::test_b"]}
    # Only the pytest summary line is trusted: log lines that merely
    # mention "passed" must not fabricate counts.
    noisy = parse_pytest_output("INFO 99 passed checks in cache warmup\nno tests ran\n")
    assert noisy["summary_found"] is False and noisy["passed"] == 0


def test_analyzer_finds_failing_tests_and_maps_sources(tmp_path):
    root = make_repo(tmp_path / "repo")
    report = SelfAnalyzer(root).analyze(**COLLECT)
    kinds = {e.kind for e in report.evidence}
    assert "test_failure" in kinds and "latency" in kinds
    assert report.weaknesses, "a failing test must yield a weakness"
    weak = report.weaknesses[0]
    assert weak.category == "test_failure"
    assert weak.affected_files == ["forge/mathy.py"]
    assert weak.evidence_ids and all(
        any(e.id == eid for e in report.evidence) for eid in weak.evidence_ids)
    assert (root / ".forge" / "self_improvement" / "analysis.json").is_file()
    assert report.metrics["failing_tests"] == 1


def test_evidence_covers_every_required_source(tmp_path):
    root = make_repo(tmp_path / "repo", broken=False)
    collector = EvidenceCollector(root)

    class Ledger:
        def top(self, limit):
            return [{"fingerprint": "boom", "category": "task", "error": "boom in forge.other",
                     "count": 4, "first_seen": 1.0, "last_seen": 2.0},
                    {"fingerprint": "agent", "category": "agent", "error": "agent died",
                     "count": 1, "first_seen": 1.0, "last_seen": 2.0}]

    collector.from_failure_ledger(Ledger())
    run = SimpleNamespace(id="r1", status="FAILED", error="model returned invalid json",
                          model="m", provider="p", attempts=2, started_at=10.0,
                          finished_at=200.0, report=lambda: {"retries": 3})
    collector.from_runs([run])
    collector.from_router_history([
        {"model": "bad", "success": False, "failure": True, "latency": 100, "error": "timeout"},
        {"model": "bad", "success": False, "failure": True, "latency": 100, "error": "timeout"},
        {"model": "good", "success": True, "failure": False, "latency": 50,
         "fallback": True, "fallback_reason": "relaxed=free"},
    ])
    collector.from_agent_runs([{"agent": "planner", "role": "planner", "success": False,
                                "error": "no plan", "elapsed_ms": 5}])
    collector.from_metrics({"latencies": {"stage.tests": {"count": 3, "p95_ms": 9000.0}},
                            "gauges": {"active_runs": 4, "max_workers": 4},
                            "counters": {"tasks_queued": 20, "tasks_started": 10}})
    collector.from_resources(disk_free_bytes=1024, thread_count=999)
    kinds = {e.kind for e in collector.items()}
    assert kinds == {"failure", "test_failure", "latency", "model_performance",
                     "routing_mistake", "repeated_error", "agent_failure",
                     "resource_bottleneck"} - {"test_failure"}
    report = SelfAnalyzer(root).analyze(failure_ledger=Ledger(), runs=[run],
                                        router_history=[], agent_runs=[], measure_host=False)
    categories = {w.category for w in report.weaknesses}
    assert "repeated_error" in categories
    # Evidence is real: the repeated error names the module it mentions.
    repeated = next(w for w in report.weaknesses if w.category == "repeated_error")
    assert repeated.affected_files == ["forge/other.py"]


def test_analysis_is_honest_when_nothing_is_wrong(tmp_path):
    root = make_repo(tmp_path / "repo", broken=False)
    report = SelfAnalyzer(root).analyze(**COLLECT)
    assert not any(e.kind == "test_failure" for e in report.evidence)
    assert report.weaknesses == []


# -- 2. proposals ----------------------------------------------------------------

def test_proposals_carry_every_required_field(tmp_path):
    root = make_repo(tmp_path / "repo")
    report = SelfAnalyzer(root).analyze(**COLLECT)
    proposals = ProposalGenerator().generate(report)
    assert proposals
    p = proposals[0]
    for name in ("hypothesis", "expected_benefit", "risk", "test_plan",
                 "affected_files", "evidence_ids"):
        assert getattr(p, name), name
    assert p.risk in ("low", "medium", "high")
    assert validate_proposal(p, [e.id for e in report.evidence]) == []


def test_invalid_proposals_are_rejected():
    empty = ImprovementProposal(id="P", title="", hypothesis="", evidence_ids=[],
                                expected_benefit="", risk="bogus", risk_notes="",
                                affected_files=[], test_plan=[])
    problems = validate_proposal(empty)
    assert "missing title" in problems and "no evidence cited" in problems
    assert "no affected files declared" in problems and "no test plan" in problems
    assert any("risk must be" in p for p in problems)
    protected = ImprovementProposal(
        id="P2", title="t", hypothesis="h", evidence_ids=["EV-0001"],
        expected_benefit="b", risk="low", risk_notes="",
        affected_files=["forge/security/policy_gate.py"], test_plan=["x"])
    assert any("guardrail" in p for p in validate_proposal(protected))
    unknown = validate_proposal(
        ImprovementProposal(id="P3", title="t", hypothesis="h", evidence_ids=["EV-9999"],
                            expected_benefit="b", risk="low", risk_notes="",
                            affected_files=["forge/x.py"], test_plan=["x"]),
        known_evidence=["EV-0001"])
    assert any("unknown evidence" in p for p in unknown)


def test_proposals_suppressed_after_two_rejections(tmp_path):
    root = make_repo(tmp_path / "repo")
    report = SelfAnalyzer(root).analyze(**COLLECT)
    weak_id = report.weaknesses[0].id
    history = [{"weakness_id": weak_id, "outcome": "rejected"}] * 2
    assert ProposalGenerator(history=history).generate(report) == []


# -- 3. candidate system ---------------------------------------------------------

def test_candidate_is_isolated_and_live_tree_untouched(tmp_path):
    root = make_repo(tmp_path / "repo")
    report = SelfAnalyzer(root).analyze(**COLLECT)
    proposal = ProposalGenerator().generate(report)[0]
    runner = CandidateRunner(root, workspace=tmp_path / "ws")
    candidate = runner.create(proposal)
    assert Path(candidate.root) != root and Path(candidate.baseline_root) != root
    assert not (Path(candidate.root) / ".forge").exists()
    runner.apply(candidate, proposal, fixing_producer)
    assert candidate.status == "applied"
    assert (Path(candidate.root) / "forge" / "mathy.py").read_text() == FIXED
    assert (root / "forge" / "mathy.py").read_text() == BROKEN, "live tree must not change"
    assert (Path(candidate.baseline_root) / "forge" / "mathy.py").read_text() == BROKEN
    runner.evaluate(candidate, proposal)
    assert candidate.status == "evaluated"
    assert candidate.tests.ok and not candidate.baseline_tests.ok
    assert candidate.comparison.improvement > 0 and not candidate.comparison.regressed
    assert candidate.security["passed"] and candidate.architecture["passed"]
    runner.cleanup(candidate)
    assert not Path(candidate.root).exists()


def test_candidate_regression_is_detected(tmp_path):
    root = make_repo(tmp_path / "repo")
    report = SelfAnalyzer(root).analyze(**COLLECT)
    proposal = ProposalGenerator().generate(report)[0]
    proposal.affected_files.append("forge/other.py")
    runner = CandidateRunner(root, workspace=tmp_path / "ws")
    candidate = runner.apply(runner.create(proposal), proposal, breaking_producer)
    runner.evaluate(candidate, proposal)
    assert candidate.status == "regressed"
    assert any("new failures" in r for r in candidate.comparison.regressions)
    decision = SelfImprovementAcceptance().decide(candidate, proposal, PolicyApproval(
        candidate.id, candidate.change_fingerprint, "alice"))
    assert not decision.accepted
    assert "regression" in decision.failed_gates and "tests" in decision.failed_gates
    runner.cleanup(candidate)


def test_candidate_rejects_undeclared_files_and_syntax_errors(tmp_path):
    root = make_repo(tmp_path / "repo")
    report = SelfAnalyzer(root).analyze(**COLLECT)
    proposal = ProposalGenerator().generate(report)[0]
    runner = CandidateRunner(root, workspace=tmp_path / "ws")
    c1 = runner.apply(runner.create(proposal), proposal,
                      lambda r, p: {"forge/other.py": "VALUE = 3\n"})
    assert c1.status == "rejected" and "undeclared" in c1.error
    c2 = runner.apply(runner.create(proposal), proposal,
                      lambda r, p: {"forge/mathy.py": "def add(:\n"})
    assert c2.status == "rejected" and "syntax error" in c2.error
    c3 = runner.apply(runner.create(proposal), proposal, lambda r, p: {})
    assert c3.status == "failed"
    for c in (c1, c2, c3):
        runner.cleanup(c)


# -- 4. acceptance ---------------------------------------------------------------

def _evaluated_candidate(tmp_path, producer=fixing_producer):
    root = make_repo(tmp_path / "repo")
    report = SelfAnalyzer(root).analyze(**COLLECT)
    proposal = ProposalGenerator().generate(report)[0]
    runner = CandidateRunner(root, workspace=tmp_path / "ws")
    candidate = runner.apply(runner.create(proposal), proposal, producer)
    runner.evaluate(candidate, proposal)
    runner.cleanup(candidate)
    return root, candidate, proposal


def test_acceptance_requires_every_gate_including_policy(tmp_path):
    _root, candidate, proposal = _evaluated_candidate(tmp_path)
    acceptance = SelfImprovementAcceptance()
    without = acceptance.decide(candidate, proposal, None)
    assert not without.accepted and without.failed_gates == ["policy"]
    assert without.awaiting_approval
    good = PolicyApproval(candidate.id, candidate.change_fingerprint, "alice")
    assert acceptance.decide(candidate, proposal, good).accepted
    # Approval bound to another candidate / fingerprint / from the loop itself.
    assert not acceptance.decide(candidate, proposal, PolicyApproval(
        "CAND-other", candidate.change_fingerprint, "alice")).accepted
    assert not acceptance.decide(candidate, proposal, PolicyApproval(
        candidate.id, "deadbeef", "alice")).accepted
    assert not acceptance.decide(candidate, proposal, PolicyApproval(
        candidate.id, candidate.change_fingerprint, "forge")).accepted
    expired = PolicyApproval(candidate.id, candidate.change_fingerprint, "alice",
                             issued_at=0.0, ttl_seconds=1.0)
    assert not acceptance.decide(candidate, proposal, expired).accepted
    # Safe/locked modes never accept even with approval.
    for mode in (OperationMode.SAFE, OperationMode.LOCKED):
        decision = SelfImprovementAcceptance(mode=mode).decide(candidate, proposal, good)
        assert not decision.accepted and "policy" in decision.failed_gates
        assert not decision.awaiting_approval


def test_acceptance_rejects_no_measurable_improvement(tmp_path):
    _root, candidate, proposal = _evaluated_candidate(tmp_path, noop_producer)
    decision = SelfImprovementAcceptance().decide(candidate, proposal, PolicyApproval(
        candidate.id, candidate.change_fingerprint, "alice"))
    assert not decision.accepted and "improvement" in decision.failed_gates


def test_security_and_architecture_gates_block(tmp_path):
    _root, candidate, proposal = _evaluated_candidate(tmp_path)
    approval = PolicyApproval(candidate.id, candidate.change_fingerprint, "alice")
    candidate.security = {"passed": False, "findings": [{"file": "x", "rule": "secret"}]}
    d = SelfImprovementAcceptance().decide(candidate, proposal, approval)
    assert not d.accepted and "security" in d.failed_gates
    candidate.security = {"passed": True}
    candidate.architecture = {"passed": False, "issues": ["new dependency cycle: a -> b -> a"]}
    d = SelfImprovementAcceptance().decide(candidate, proposal, approval)
    assert not d.accepted and "architecture" in d.failed_gates


# -- 5/6. ledger and rollback ----------------------------------------------------

def test_ledger_is_append_only_and_hash_chained(tmp_path):
    ledger = ImprovementLedger(tmp_path)
    ledger.record("note", {"a": 1})
    ledger.record("note", {"token": "sk-abcdefghijklmnopqrstuvwxyz123456"})
    entries = ledger.entries()
    assert [e["seq"] for e in entries] == [1, 2]
    assert entries[1]["prev"] == entries[0]["hash"]
    assert "sk-abcdefghijklmnop" not in json.dumps(entries), "ledger must redact secrets"
    assert ledger.verify_chain() == (True, "ok")
    lines = ledger.path.read_text().splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["a"] = 2
    ledger.path.write_text(json.dumps(tampered) + "\n" + lines[1] + "\n")
    ok, detail = ledger.verify_chain()
    assert not ok and "seq 1" in detail
    with pytest.raises(ValueError):
        ledger.record("bogus", {})


def test_rollback_restores_exact_originals_and_removes_created_files(tmp_path):
    root = make_repo(tmp_path / "repo")
    unrelated = root / "forge" / "unrelated.py"
    manager = RollbackManager(root)
    result = manager.apply("CAND-1", {"forge/mathy.py": FIXED, "forge/newmod.py": "X = 1\n"})
    assert sorted(result["files"]) == ["forge/mathy.py", "forge/newmod.py"]
    assert (root / "forge" / "mathy.py").read_text() == FIXED
    unrelated.write_text("# user work after apply\n")
    assert manager.can_rollback("CAND-1")
    rolled = manager.rollback("CAND-1")
    assert rolled["restored"] == ["forge/mathy.py"] and rolled["removed"] == ["forge/newmod.py"]
    assert (root / "forge" / "mathy.py").read_text() == BROKEN
    assert not (root / "forge" / "newmod.py").exists()
    assert unrelated.read_text() == "# user work after apply\n", "unrelated work preserved"
    assert manager.list()[0]["rolled_back"] is True
    with pytest.raises(FileNotFoundError):
        manager.rollback("CAND-missing")
    with pytest.raises(FileExistsError):
        manager.apply("CAND-1", {"forge/mathy.py": FIXED})


def test_rollback_refuses_corrupt_snapshot(tmp_path):
    root = make_repo(tmp_path / "repo")
    manager = RollbackManager(root)
    manager.apply("CAND-2", {"forge/mathy.py": FIXED})
    snapshot = manager.snapshots / "CAND-2"
    blob = next(snapshot.glob("*.blob"))
    blob.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        manager.rollback("CAND-2")


# -- 7. bounded iterations and full loop ----------------------------------------

def test_engine_full_loop_parks_for_approval_then_applies_and_rolls_back(tmp_path):
    root = make_repo(tmp_path / "repo")
    engine = make_engine(root, fixing_producer, max_iterations=2)
    results = engine.run(collect_kwargs=COLLECT)
    assert len(results) == 1
    result = results[0]
    assert not result.accepted, "no auto-acceptance without a human approval"
    assert result.decision.awaiting_approval
    assert result.decision.failed_gates == ["policy"]
    assert (root / "forge" / "mathy.py").read_text() == BROKEN, "loop never writes live tree"
    status = engine.status()
    assert status.pending_approval == [result.candidate.id]
    cid = result.candidate.id
    # Apply refused before approval.
    with pytest.raises(PermissionError):
        engine.apply_accepted(cid, PolicyApproval(cid, result.candidate.change_fingerprint, "alice"))
    with pytest.raises(PermissionError):
        engine.approve(cid, approved_by="alice", change_fingerprint="wrong")
    approval, decision = engine.approve(cid, approved_by="alice", reason="reviewed diff")
    assert decision.accepted
    applied = engine.apply_accepted(cid, approval)
    assert applied["committed"] is False
    assert (root / "forge" / "mathy.py").read_text() == FIXED
    assert engine.pending == {}
    kinds = [e["kind"] for e in engine.ledger.entries()]
    for kind in ("analysis", "proposal", "candidate", "decision", "pending", "iteration", "applied"):
        assert kind in kinds
    assert engine.ledger.verify_chain()[0]
    rolled = engine.rollback(cid, actor="alice")
    assert rolled["restored"] == ["forge/mathy.py"]
    assert (root / "forge" / "mathy.py").read_text() == BROKEN
    assert engine.status().rollbacks == 1
    dashboard = engine.dashboard()
    assert dashboard["applied"] and dashboard["rollbacks"] and dashboard["proposals"]
    assert dashboard["evidence"] and dashboard["weaknesses"]


def test_apply_refuses_when_live_file_drifted(tmp_path):
    root = make_repo(tmp_path / "repo")
    engine = make_engine(root, fixing_producer)
    result = engine.run(collect_kwargs=COLLECT)[0]
    cid = result.candidate.id
    approval, _ = engine.approve(cid, approved_by="alice")
    (root / "forge" / "mathy.py").write_text(BROKEN + "# edited meanwhile\n")
    with pytest.raises(PermissionError, match="changed since"):
        engine.apply_accepted(cid, approval)


def test_regressing_candidates_are_rejected_and_ledgered(tmp_path):
    root = make_repo(tmp_path / "repo")

    def producer(r, p):
        p.affected_files.append("forge/other.py")
        return breaking_producer(r, p)

    engine = make_engine(root, producer)
    result = engine.run(collect_kwargs=COLLECT)[0]
    assert not result.accepted and not result.decision.awaiting_approval
    assert "regression" in result.decision.failed_gates
    assert engine.status().rejected == 1 and engine.status().pending_approval == []
    assert engine.dashboard()["rejected"][0]["failed_gates"]


def test_iteration_count_is_hard_bounded(tmp_path):
    root = make_repo(tmp_path / "repo", broken=False)
    engine = SelfImprovementEngine(root, producer=noop_producer, max_iterations=999)
    assert engine.max_iterations == HARD_MAX_ITERATIONS
    calls = []

    def producer(r, p):
        calls.append(p.id)
        return noop_producer(r, p)

    # Fake analysis with many weaknesses to make sure the bound applies.
    from forge.self_improvement.analysis import SelfAnalysisReport, Weakness
    from forge.self_improvement.evidence import Evidence

    class ManyAnalyzer:
        output_dir = root / ".forge" / "self_improvement"

        def analyze(self, **kwargs):
            evidence = [Evidence(id=f"EV-{i:04d}", kind="failure", source="t", summary=f"e{i}")
                        for i in range(1, 30)]
            weaknesses = [Weakness(id=f"WEAK-{i:03d}", category="failure", severity="low",
                                   title=f"w{i}", metric="failures", value=1, target=0,
                                   evidence_ids=[f"EV-{i:04d}"], affected_files=["forge/mathy.py"],
                                   suggested_action=f"fix {i}") for i in range(1, 30)]
            return SelfAnalysisReport(root=str(root), generated_at=0.0, evidence=evidence,
                                      weaknesses=weaknesses)

    engine = SelfImprovementEngine(root, producer=producer, analyzer=ManyAnalyzer(),
                                   max_iterations=2)
    results = engine.run(max_iterations=50, stop_on_accept=False)
    assert len(results) == HARD_MAX_ITERATIONS and len(calls) == HARD_MAX_ITERATIONS
    assert "iteration bound" in results[-1].stopped_reason
    results = engine.run(max_iterations=2, stop_on_accept=False)
    assert len(results) == 2


def test_engine_reports_honestly_when_nothing_to_improve(tmp_path):
    root = make_repo(tmp_path / "repo", broken=False)
    results = make_engine(root, fixing_producer).run(collect_kwargs=COLLECT)
    assert len(results) == 1 and results[0].proposal is None
    assert results[0].stopped_reason == "no admissible proposals"


# -- 8. the six "never" guardrails ---------------------------------------------

def test_never_modify_protected_files_without_authorization():
    rails = Guardrails()
    for path in ("forge/security/policy.py", ".forge/cockpit.db", ".git/config",
                 "pyproject.toml", "forge/self_improvement/guardrails.py",
                 "forge/self_improvement/acceptance.py", "../outside.py", ".github/x.yml"):
        assert rails.check_paths([path]), path
    assert rails.check_paths(["forge/agents/planner.py"]) == []
    auth = ProtectedFileAuthorization(paths=("forge/security/review.py",), authorized_by="alice")
    assert Guardrails(authorization=auth).check_paths(["forge/security/review.py"]) == []
    assert Guardrails(authorization=auth).check_paths(["forge/security/policy.py"])
    # The loop cannot authorize itself: an empty approver is worthless.
    blank = ProtectedFileAuthorization(paths=("forge/security/review.py",), authorized_by="")
    assert Guardrails(authorization=blank).check_paths(["forge/security/review.py"])


def test_never_modify_credentials_even_with_authorization():
    auth = ProtectedFileAuthorization(paths=(".env", "config/credentials.json", "id_rsa"),
                                      authorized_by="alice")
    rails = Guardrails(authorization=auth)
    for path in (".env", "config/credentials.json", "id_rsa", "keys/server.pem",
                 "forge/models/api_key.txt"):
        violations = rails.check_paths([path])
        assert violations and violations[0]["rule"] == "credential file", path
    content = rails.check_content("forge/x.py", 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz"\n')
    assert any(v["rule"] == "credential material" for v in content)


def test_never_increase_own_permissions_or_mode():
    rails = Guardrails()
    escalations = [
        '"run_command": PermissionLevel.SAFE,',
        "PermissionLevel.BLOCKED -> PermissionLevel.SAFE",
        "manager = PermissionManager(mode=OperationMode.AUTONOMOUS)",
        'mode = "autonomous"',
    ]
    for text in escalations:
        assert rails.check_content("forge/core/supervisor.py", text), text
    # Text that already existed is not a *new* escalation.
    assert rails.check_content("forge/x.py", 'mode = "autonomous"\n', original='mode = "autonomous"\n') == []


def test_never_disable_policy_or_bypass_approvals():
    rails = Guardrails()
    for text in ("self.policy = None\n", "gate = None\n", "skip_policy = True",
                 "bypass_approvals()", "approved = True", "auto_approve = True",
                 "require_approval = False", "disable_security()",
                 "policy_approval = PolicyApproval(candidate.id, fp, 'forge')"):
        assert rails.check_content("forge/core/x.py", text), text


def test_never_remove_own_security_controls():
    rails = Guardrails()
    original = ("from forge.security.policy_gate import PolicyGate\n"
                "gate = PolicyGate(manager)\n"
                "outcome = gate.evaluate(operation='write_file')\n")
    stripped = "outcome = None\n"
    violations = rails.check_content("forge/tools/x.py", stripped, original=original)
    assert any(v["rule"] == "security control removed" and v["detail"].startswith("PolicyGate")
               for v in violations)
    # Keeping the control is fine.
    assert rails.check_content("forge/tools/x.py", original + "# more\n", original=original) == []


def test_guardrails_enforced_on_candidate_and_apply(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "forge" / "security").mkdir()
    (root / "forge" / "security" / "policy.py").write_text("RULES = 1\n")
    report = SelfAnalyzer(root).analyze(**COLLECT)
    proposal = ProposalGenerator().generate(report)[0]
    runner = CandidateRunner(root, workspace=tmp_path / "ws")
    # A producer that tries to weaken approvals inside a declared file.
    cand = runner.apply(runner.create(proposal), proposal,
                        lambda r, p: {"forge/mathy.py": FIXED + "approved = True\n"})
    assert cand.status == "rejected" and cand.guardrail_violations
    assert any(v["rule"] == "approval bypass" for v in cand.guardrail_violations)
    decision = SelfImprovementAcceptance().decide(cand, proposal, PolicyApproval(
        cand.id, cand.change_fingerprint or "x", "alice"))
    assert not decision.accepted and "guardrails" in decision.failed_gates
    runner.cleanup(cand)
    # Direct apply through the rollback manager is guarded too.
    with pytest.raises(GuardrailViolation):
        RollbackManager(root).apply("CAND-x", {"forge/security/policy.py": "RULES = 0\n"})
    assert (root / "forge" / "security" / "policy.py").read_text() == "RULES = 1\n"
    # And a proposal declaring protected files never becomes a candidate.
    proposal.affected_files = ["forge/security/policy.py"]
    engine = make_engine(root, fixing_producer)
    engine.last_report = report
    with pytest.raises(GuardrailViolation):
        engine.evaluate_proposal(proposal, report=report)


def test_guardrail_invariants_are_documented():
    described = Guardrails.describe()
    for phrase in ("remove its own security controls", "increase its own permissions",
                   "disable policy", "bypass approvals", "modify credentials",
                   "protected files without explicit authorization"):
        assert any(phrase in inv for inv in described["invariants"]), phrase
    assert "forge/self_improvement/guardrails.py" in described["protected_paths"]


# -- hardening: fakes removed, gates tightened -------------------------------------------


def test_guardrails_flag_test_weakening_and_assert_removal():
    rails = Guardrails()
    weak = "import pytest\n\ndef test_x():\n    pytest.skip('flaky')\n    assert True\n"
    rules = {v["rule"] for v in rails.check_content("tests/test_x.py", weak)}
    assert "test weakening" in rules
    original = "def test_y():\n    assert a == 1\n    assert b == 2\n"
    fewer = "def test_y():\n    assert a == 1\n"
    violations = rails.check_content("tests/test_y.py", fewer, original=original)
    assert any(v["rule"] == "test weakening" and "2 -> 1" in v["detail"] for v in violations)


def test_router_fallbacks_come_from_telemetry_route_events():
    from forge.self_improvement.evidence import EvidenceCollector, EvidenceKind
    collector = EvidenceCollector(".")
    events = [{"kind": "route", "model": "m1", "capability": "code", "fallback": True,
               "fallback_reason": "timeout", "error": ""} for _ in range(3)]
    collector.from_router_history([], route_events=events)
    kinds = [e.kind for e in collector.items()]
    assert EvidenceKind.ROUTING_MISTAKE.value in kinds
    routing = [e for e in collector.items() if e.kind == EvidenceKind.ROUTING_MISTAKE.value][0]
    assert routing.measurement.get("count") == 3


def test_metrics_evidence_uses_real_counters_and_seconds():
    from forge.self_improvement.evidence import EvidenceCollector, EvidenceKind
    collector = EvidenceCollector(".")
    snapshot = {"counters": {"runs.failed": 6, "runs.succeeded": 4},
                "gauges": {"active_runs": 4, "max_workers": 4, "queued_runs": 7},
                "histograms": {}}
    collector.from_metrics(snapshot)
    kinds = {e.kind for e in collector.items()}
    assert EvidenceKind.REPEATED_ERROR.value in kinds
    assert EvidenceKind.RESOURCE_BOTTLENECK.value in kinds
    # Unknown/legacy counter names must not fabricate evidence.
    quiet = EvidenceCollector(".")
    quiet.from_metrics({"counters": {"tasks_queued": 900}, "gauges": {}, "histograms": {}})
    assert quiet.items() == []


def test_ledger_rotation_keeps_chain_and_history(tmp_path, monkeypatch):
    from forge.self_improvement import ledger as ledger_mod
    monkeypatch.setattr(ledger_mod, "MAX_ENTRIES", 3)
    ledger = ledger_mod.ImprovementLedger(tmp_path)
    for i in range(7):
        ledger.record("analysis", {"i": i})
    ok, _ = ledger.verify_chain()
    assert ok
    rotated = sorted(ledger.path.parent.glob("ledger.*.jsonl"))
    assert rotated, "old entries must be rotated, never truncated"
    total = sum(1 for f in rotated + [ledger.path] for _ in f.read_text().splitlines())
    assert total == 7
    # Tampering with an entry is detected.
    lines = ledger.path.read_text().splitlines()
    lines[-1] = lines[-1].replace('"i": 6', '"i": 60')
    ledger.path.write_text("\n".join(lines) + "\n")
    assert ledger.verify_chain()[0] is False


def test_candidate_rejects_undeclared_writes(tmp_path):
    from forge.self_improvement.candidate import CandidateRunner
    from forge.self_improvement.proposals import ImprovementProposal
    root = tmp_path / "repo"
    (root / "forge").mkdir(parents=True)
    (root / "forge" / "__init__.py").write_text("")
    (root / "forge" / "a.py").write_text("X = 1\n")
    (root / "forge" / "b.py").write_text("Y = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("from forge.a import X\n\ndef test_a():\n    assert X == 1\n")
    runner = CandidateRunner(root)
    proposal = ImprovementProposal(
        id="P-sneaky", weakness_id="w1", title="t", hypothesis="h", evidence_ids=["e1"],
        expected_benefit="b", risk="low", risk_notes="", affected_files=["forge/a.py"],
        test_plan=["tests/test_a.py"], category="reliability")
    candidate = runner.create(proposal)

    def sneaky(candidate_root, prop):
        (candidate_root / "forge" / "b.py").write_text("Y = 2\n")
        return {"forge/a.py": "X = 1  # touched\n"}

    runner.apply(candidate, proposal, sneaky)
    assert candidate.status == "rejected"
    assert any(v["rule"] == "undeclared write" for v in candidate.guardrail_violations)
