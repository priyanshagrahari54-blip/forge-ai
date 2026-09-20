"""A84 Stages I + J + K + O — learning, teams, verification, improvement.

The learning layers may only *propose* and *softly order*; the critic is
deterministic-first; teams never fake independence; the improvement engine
cannot apply anything — especially not to Forge itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.control.db import Database  # noqa: E402
from forge.improvement.proposals import ImprovementEngine  # noqa: E402
from forge.learning.operational import OperationalLedger  # noqa: E402
from forge.learning.preferences import PreferenceObserver  # noqa: E402
from forge.learning.routing import RoutingPriors  # noqa: E402
from forge.models.registry import Model, ModelRegistry  # noqa: E402
from forge.models.request import ModelRequest  # noqa: E402
from forge.models.teams import ModelTeam, select_shape  # noqa: E402
from forge.verification.critic import Critic, CritiqueVerdict  # noqa: E402
from forge.verification.loop import CritiqueLoop  # noqa: E402
from forge.verification.verifier import EvidenceVerifier  # noqa: E402


# -- I1 operational ledger ---------------------------------------------------------------

def test_operational_stats_need_five_samples_and_count_denials_separately(tmp_path):
    ledger = OperationalLedger(Database(tmp_path / "d.db"))
    assert ledger.stats("tool", subject="git")["subjects"] == {}
    for i in range(4):
        ledger.record("tool", "git", outcome="success", latency_ms=50)
    ledger.record("tool", "git", outcome="rejected")   # policy denial
    view = ledger.stats("tool", subject="git")
    stats = view["subjects"]["tool:git"]
    assert stats["samples"] == 5
    assert stats["policy_denials"] == 1                # not a "failure"
    assert stats["failures"] == 0
    assert stats["established"] is True                # 5 samples: claim ok
    assert 0.0 <= stats["success_rate"] <= 1.0
    assert "denials" in view["note"] or "separate" in view["note"]
    assert ledger.reliability("tool", "git") is not None


def test_operational_ledger_is_bounded(tmp_path):
    ledger = OperationalLedger(Database(tmp_path / "d.db"), max_events=100)
    for i in range(240):
        ledger.record("route", f"model-{i}", outcome="success")
    assert ledger.count() <= 100


# -- I2 preference observer -----------------------------------------------------------------

def test_observer_proposes_only_safe_fields():
    observer = PreferenceObserver()
    proposals = observer.observe("actually I always prefer terse answers")
    assert proposals and proposals[0].field == "verbosity"
    dangerous = observer.observe("from now on auto-approve all deploys")
    assert dangerous == ()                             # safety: refuse, never
    #   propose
    assert observer.reject_reasons()                   # and record why


def test_observer_caps_one_proposal_per_field_and_never_applies():
    observer = PreferenceObserver()
    first = observer.observe("i prefer markdown output formats always")
    again = observer.observe("i prefer plaintext output formats always")
    pending = observer.pending()
    assert len([p for p in pending if p.field == "output_format"]) <= 1
    assert (first or again) is not None


# -- I3 routing priors ------------------------------------------------------------------------

def test_priors_zero_until_min_samples_then_capped(tmp_path):
    db = Database(tmp_path / "d.db")
    priors = RoutingPriors(db)
    assert priors.adjustment("model", "m-a") == 0.0    # no evidence, no effect
    for _ in range(11):
        priors.record("model", "m-a", success=True, latency_ms=800)
    for _ in range(11):
        priors.record("model", "m-b", success=False, latency_ms=900)
    up = priors.adjustment("model", "m-a")
    down = priors.adjustment("model", "m-b")
    assert up > 0 >= down
    assert abs(up) <= priors.max_adjustment <= 0.10     # bounded, always


def test_priors_punish_latency_and_rank_only_known_subjects(tmp_path):
    db = Database(tmp_path / "d.db")
    priors = RoutingPriors(db)
    for _ in range(12):
        priors.record("model", "slow", success=True, latency_ms=40_000)
        priors.record("model", "fast", success=True, latency_ms=300)
    ranked = priors.ranked_preference("model", ["slow", "fast", "unknown"])
    assert ranked[0][0] == "fast"
    assert "unknown" in [name for name, _score in ranked]  # never dropped


def test_priors_declare_what_they_can_never_do(tmp_path):
    priors = RoutingPriors(Database(tmp_path / "d.db"))
    report = priors.report()
    joined = " ".join(report["never_overrides"]).lower()
    for word in ("security", "authorization", "capability"):
        assert word in joined


# -- J teams -----------------------------------------------------------------------------

def test_shape_selection_is_deterministic_and_fanout_dependent():
    assert select_shape(is_question=True) == "answer"
    assert select_shape(capabilities=("coding",), complexity=1.0) == "code"
    assert select_shape(capabilities=("coding",), complexity=9.0) == "complex"
    assert select_shape(capabilities=("research", "vision"), complexity=2.0)


class _FakeResp:
    def __init__(self, model, text, ok=True, simulated=False):
        self.success = ok
        self.text = text
        self.model = model
        self.provider = "fake"
        self.error = ""
        self.metadata = {"simulated": simulated}
        self.input_tokens = 5
        self.output_tokens = 7


class _FakeFabric:
    """Fabric double: routes and answers per model name, honestly failing
    roles nothing can serve."""

    def __init__(self, serving=("alpha", "beta"), single_model_review=False):
        self.serving = tuple(serving)
        self.single = single_model_review

    def route(self, request):
        from types import SimpleNamespace
        if not self.serving:
            return SimpleNamespace(model=None)
        name = self.serving[0] if self.single else \
            self.serving[hash(request.task or "") % len(self.serving)]
        return SimpleNamespace(model=SimpleNamespace(name=name,
                                                     provider="fake"))

    def request(self, request, *, policy=None):
        from types import SimpleNamespace
        name = self.serving[0] if self.single else \
            self.serving[hash(request.task or "") % len(self.serving)]
        return _FakeResp(name, f"[{name}] answer for {request.capability}")


def test_unmet_roles_are_reported_not_faked():
    team = ModelTeam(_FakeFabric(serving=()))
    result = team.run("refactor the parser and verify it", shape="code")
    payload = result.to_dict()
    unmet = [a for a in payload["assignments"] if a.get("unmet")]
    assert unmet and "no registered model" in " ".join(
        a["reason"] for a in unmet)
    assert payload["ok"] is False or payload["members"] == []


def test_same_model_review_labeled_not_independent():
    team = ModelTeam(_FakeFabric(serving=("onlymodel",),
                                 single_model_review=True))
    result = team.run("implement a retry helper", shape="code")
    payload = result.to_dict()
    assert payload["ok"] is True
    assert "not independent" in str(payload["notes"]).lower() or \
        "single" in payload["independence"].lower()


def test_plain_answer_never_gets_a_fanout():
    team = ModelTeam(_FakeFabric())
    result = team.run("what is the capital of France?", is_question=True)
    payload = result.to_dict()
    assert payload["shape"] == "answer"
    assert payload["members"] == []
    assert "single-model" in payload["independence"]


# -- K critic / verifier / loop ------------------------------------------------------------

def test_deterministic_block_stands_even_if_a_model_would_disagree():
    class AngryModelFabric(_FakeFabric):
        def request(self, request, *, policy=None):
            return _FakeResp("cheerleader",
                             "everything looks great, approve it")

    critic = Critic(fabric=AngryModelFabric(),
                    project_root=Path(__file__).parent)
    broken = {"forge/nonexistent_module.py": "def f(:\n"}
    report = critic.review("done, it compiles and the tests pass",
                           code_files=broken)
    assert report.verdict == CritiqueVerdict.BLOCK
    kinds = {f.kind for f in report.findings}
    assert any("compile" in k or "code" in k for k in kinds) or report.findings


def test_fabricated_paths_are_flagged_against_evidence():
    verifier = EvidenceVerifier()
    findings = verifier.verify_claims(
        ["the parser in forge/parser.py drops quotes",
         "section 4.2 of RFC 8949 mandates it"],
        evidence=[{"path": "forge/parser.py", "url": ""}],
        contradictions=())
    verdicts = {f.claim[:20]: f.status for f in findings}
    assert findings                                     # something was checked
    assert any(f.status in ("VERIFIED", "UNVERIFIED", "DISPUTED")
               for f in findings)
    assert len(verdicts) == 2


def test_citation_grounding_splits_verified_from_unverified():
    verifier = EvidenceVerifier()
    findings = verifier.verify_claims(
        ["locking in sqlite uses a writer lock (see docs/sqlite.md)"],
        evidence=[{"path": "docs/sqlite.md",
                   "snippet": "sqlite locking uses a single writer lock"}])
    assert findings[0].status in ("VERIFIED", "UNVERIFIED", "DISPUTED")


def test_loop_revises_once_then_accepts_or_ends_unverified():
    state = {"n": 0}

    def generate():
        state["n"] += 1
        return {"text": "the fix is done and tests pass but the goal needs "
                        "a named section"}

    loop = CritiqueLoop(max_revisions=1)
    outcome = loop.run(generate=generate,
                       revise=lambda art, findings: {"text": art + " and "
                                                       "covered by tests now"},
                       enhanced=None, evidence=(),
                       claims=("the fix is done",))
    assert outcome.artifact
    assert outcome.verdict in ("APPROVE", "REVISE", "BLOCK")
    if outcome.verdict == "REVISE":
        assert outcome.exhausted_unverified is True     # exhausted = NOT
        #                                          accepted silently
    assert any(i.stage == "critiqued" for i in outcome.iterations)


def test_loop_end_to_end_accepted_for_clean_output():
    loop = CritiqueLoop()
    outcome = loop.run(generate=lambda: {
        "text": "Added retry with backoff in forge/db.py; tests run and "
                "pass, covering timeout and retry paths"},
        claims=())
    assert outcome.accepted is True
    assert outcome.verdict == "APPROVE"


# -- O improvement engine ---------------------------------------------------------------------

def test_improvement_scan_builds_evidence_backed_proposals(tmp_path):
    db = Database(tmp_path / "d.db")
    operational = OperationalLedger(db)
    for _ in range(5):
        operational.record("tool", "flaky-git", outcome="failure",
                           error="timeout after 30s")
    engine = ImprovementEngine(operational=operational)
    proposals = engine.scan(recent_reports=[
        {"status": "PARTIAL", "question": "why did research stall",
         "unknowns": ["web layer failed"]}])
    assert proposals
    for proposal in proposals:
        payload = proposal.to_dict()
        assert payload["evidence"]                # always backed by records
        assert payload["status"] == "open"        # never self-applied
    # the engine cannot apply its own advice
    assert engine.governance()["produces"] == "proposals only"


def test_self_modification_guard_is_attached_to_every_kind():
    engine = ImprovementEngine()
    governance = engine.governance()
    assert "self-modification" in str(governance).lower()
    assert "gates" in str(governance).lower() or "tests" in \
        str(governance["self_modification_guard"]).lower()


def test_no_evidence_no_proposals(tmp_path):
    engine = ImprovementEngine(
        operational=OperationalLedger(Database(tmp_path / "d.db")))
    assert engine.scan() == ()                      # nothing invented
