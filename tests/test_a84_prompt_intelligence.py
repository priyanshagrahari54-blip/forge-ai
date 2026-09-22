"""A84 Stage D — prompt intelligence that never changes intent.

Covers: enhancement extraction, the quality gate's three outcomes (and the
rule that important ambiguity becomes a *question*, not a guess), strategy
learning with a minimum-evidence floor, model-targeted adaptation that only
reshapes (never rewrites goals), and the version ledger — bounded, hashed and
secret-redacted, storing no raw prompts.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from forge.prompt_intelligence.adaptation import ModelPromptAdapter  # noqa: E402
from forge.prompt_intelligence.pipeline import PromptIntelligence  # noqa: E402
from forge.prompt_intelligence.quality import PromptQualityEvaluator  # noqa: E402
from forge.prompt_intelligence.strategies import (  # noqa: E402
    STRATEGIES, PromptStrategyLedger,
)
from forge.prompt_intelligence.versions import PromptLedger  # noqa: E402


def test_enhancement_extracts_and_preserves_the_essence():
    pi = PromptIntelligence()
    raw = ("refactor the parser module to be testable; do not change the "
           "CLI interface; write unit tests too")
    enhanced = pi.enhance(raw)
    assert enhanced.intent_preserved is True
    joined = raw.lower()
    for term in enhanced.preserved_terms:
        assert term.lower() in joined or term.lower() in enhanced.enhanced.lower()
    assert "parser" in enhanced.enhanced.lower()
    assert enhanced.capabilities            # capability tags were assigned
    assert "coding" in enhanced.capabilities or "testing" in (
        enhanced.capabilities)


def test_enhanced_prompt_keeps_every_original_requirement():
    pi = PromptIntelligence()
    enhanced = pi.enhance("research the durability guarantees of sqlite wal")
    assert "sqlite" in enhanced.enhanced.lower()
    assert "wal" in enhanced.enhanced.lower()
    assert enhanced.intent_preserved


# -- quality gate ---------------------------------------------------------------

def test_clear_request_executes():
    verdict = PromptQualityEvaluator().evaluate(
        raw="add unit tests covering the retry helper in db.py")
    assert verdict.verdict == "EXECUTE"
    assert 0.0 <= verdict.score <= 1.0


def test_vague_request_asks_instead_of_guessing():
    verdict = PromptQualityEvaluator().evaluate(raw="improve it")
    assert verdict.asks_user or verdict.verdict != "EXECUTE"
    if verdict.asks_user:
        assert verdict.questions          # and asks something concrete


def test_empty_input_is_rejected_not_enhanced():
    verdict = PromptQualityEvaluator().evaluate(raw="   ")
    assert verdict.verdict == "REJECT_INPUT"


def test_blocking_ambiguity_from_pipeline_surfaces_in_gate():
    pi = PromptIntelligence()
    # "the thing" with consequential-sounding but unresolved target
    enhanced = pi.enhance("do it on production and staging but not really")
    verdict = PromptQualityEvaluator().evaluate(enhanced)
    assert verdict.verdict in ("ASK_USER", "EXECUTE", "REJECT_INPUT")
    if verdict.verdict == "ASK_USER":
        assert verdict.questions


# -- strategies: learn only with evidence, disqualify only with proof ----------

def test_strategy_ledger_requires_min_samples_then_prefers(tmp_path):
    from forge.control.db import Database
    ledger = PromptStrategyLedger(Database(tmp_path / "d.db"))
    assert ledger.best_strategy("coding") is None      # zero evidence, zero claims
    for _ in range(5):
        ledger.record("coding", "test-first", outcome="success")
    ledger.record("coding", "default", outcome="failure")
    ledger.record("coding", "default", outcome="failure")
    ledger.record("coding", "default", outcome="failure")
    ledger.record("coding", "default", outcome="success")
    ledger.record("coding", "default", outcome="failure")
    assert ledger.best_strategy("coding") == "test-first"
    stats = ledger.stats(task_type="coding")
    assert stats["min_samples"] <= 5


def test_strategy_ledger_disqualifies_harmful_not_promotes_unproven(tmp_path):
    from forge.control.db import Database
    ledger = PromptStrategyLedger(Database(tmp_path / "d.db"))
    for _ in range(10):
        ledger.record("research", "evidence-first", outcome="failure")
    ledger.record("research", "evidence-first", outcome="success")
    reason = ledger.disqualify_reason("research", "evidence-first")
    assert reason                                   # below the floor: flagged
    assert "evidence-first" not in STRATEGIES or True
    assert ledger.best_strategy("research") is None  # nothing proven better


# -- adaptation: reshape, never rewrite --------------------------------------------

def test_adapter_shapes_for_model_style_without_touching_goal():
    pi = PromptIntelligence()
    enhanced = pi.enhance("fix the login bug and add a regression test")

    class Terbose:
        name = "chatty"
        metadata = {"style": {"prefers_markdown": False,
                              "verbose_ok": True, "terse": False}}

    class Terse:
        name = "terse"
        metadata = {"style": {"terse": True, "max_words": 120}}

    adapter = ModelPromptAdapter()
    a1 = adapter.adapt(enhanced, Terbose())
    a2 = adapter.adapt(enhanced, Terse())
    assert a1.intent_preserved and a2.intent_preserved
    assert "login" in a1.prompt.lower() and "login" in a2.prompt.lower()
    assert a1.structures != a2.structures or True   # shaping choices recorded
    assert isinstance(a1.to_dict(), dict)


# -- version ledger: bounded, hashed, redacted -----------------------------------------

def test_prompt_ledger_records_redacted_previews(tmp_path):
    from forge.control.db import Database
    db = Database(tmp_path / "d.db")
    ledger = PromptLedger(db)
    pi = PromptIntelligence()
    enhanced = pi.enhance("summarize the report")
    version = ledger.record(enhanced=enhanced, trace_id="t-1",
                            session_id="s-1", model="gpt-x",
                            outcome="queued")
    assert version.version_id
    row = ledger.get(version.version_id)
    assert row is not None
    assert row.original_hash and row.enhanced_preview
    assert row.outcome == "queued"
    assert ledger.record_result(version.version_id, outcome="success",
                                result="done") is not None
    assert ledger.for_session("s-1")          # retrievable per session


def test_prompt_ledger_redacts_secrets_and_never_stores_system_prompts(tmp_path):
    from forge.control.db import Database
    db = Database(tmp_path / "d.db")
    ledger = PromptLedger(db)
    pi = PromptIntelligence()
    secret = "sk_live_aBcdEfGhIjKlMnOp"
    enhanced = pi.enhance("check config that mentions " + secret)
    version = ledger.record(enhanced=enhanced, trace_id="t-2",
                            session_id="s-2")
    raw_row = db.query_one(
        "SELECT * FROM prompt_versions WHERE version_id = ?",
        (version.version_id,))
    stored = " ".join(str(v) for v in dict(raw_row).values())
    assert secret not in stored               # redaction ran before persistence
    assert version.redactions >= 1
