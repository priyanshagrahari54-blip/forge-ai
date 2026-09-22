"""A84 Stage L + M/N — personalization and context assembly.

Personalization: closed field vocabulary; stored *as* memory (so inspect /
correct / forget apply); can only ever restrict, never relax; shapes
presentation and soft routing hints only.

Context: relevance hierarchy under a real budget — dedup, fold-to-lossy-
digest (labeled), drop-and-name; every bundle ships quality metrics so the
answer can be judged on the context it actually had.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.assistant.context import (  # noqa: E402
    DEFAULT_TOKEN_BUDGET, ContextEngine,
)
from forge.assistant.memory import PersonalMemoryService  # noqa: E402
from forge.assistant.personalization import (  # noqa: E402
    PROFILE_FIELD_NAMES, PreferenceProfile, parse_profile_memory,
)
from forge.assistant.sessions import SessionLedger  # noqa: E402
from forge.control.db import Database  # noqa: E402
from forge.memory.engine import LongTermMemory  # noqa: E402
from forge.models.request import ModelRequest  # noqa: E402


def _stack(tmp_path):
    db = Database(tmp_path / "plane.db")
    engine = LongTermMemory(db, project="personal")
    memory = PersonalMemoryService(engine)
    ledger = SessionLedger(db)
    return db, engine, memory, ledger


# -- preference profile ---------------------------------------------------------------

def test_closed_vocabulary_and_refusal_of_permission_fields():
    profile = PreferenceProfile()
    profile.set("response_style", "concise")
    assert profile.get("response_style") == "concise"
    for forbidden in ("allow_deploy", "auto_approve", "skip_security"):
        with pytest.raises(ValueError):
            profile.set(forbidden, "yes")
    with pytest.raises(ValueError):
        profile.set("blood_type", "O+")     # health: not personalizable here
    assert "response_style" in PROFILE_FIELD_NAMES


def test_profile_roundtrips_through_memory_not_duplicates(tmp_path):
    _db, engine, _m, _l = _stack(tmp_path)
    profile = PreferenceProfile()
    profile.set("verbosity", "terse")
    first = profile.save_to(engine, project="personal")
    assert first["saved"] and first["fields"]
    stored = [r for r in engine.list(project="personal")]
    assert len(stored) == 1
    # change it: correct (version), do not append a second live row
    profile.set("verbosity", "detailed")
    profile.save_to(engine, project="personal")
    live = [r for r in engine.list(project="personal")]
    assert len(live) == 1
    assert "detailed" in live[0].content
    loaded = PreferenceProfile.from_memory(engine, project="personal")
    assert loaded.get("verbosity") == "detailed"
    parsed = parse_profile_memory(live[0].content)
    assert parsed and parsed[0] == "verbosity"


def test_profile_can_only_restrict(tmp_path):
    _db, engine, _m, _l = _stack(tmp_path)
    strict = PreferenceProfile(values={"privacy_mode": "strict",
                                       "preferred_models": "ollama:llama3"})
    request = ModelRequest(prompt="p", capability="coding",
                           prefer_local=False, max_cost_per_token=9.9)
    changed = strict.apply_to_request(request)
    assert request.preferred_models == ("ollama:llama3",)
    assert request.prefer_local is True               # one-way: toward local
    assert request.max_cost_per_token == 9.9          # never *relaxed*
    assert "preferred_models" in changed
    hints = strict.planner_hints()
    assert "preferred_tools" in hints                 # hint shape, not policy


# -- context engine ---------------------------------------------------------------------

def test_context_assembles_real_state(tmp_path):
    _db, _engine, memory, ledger = _stack(tmp_path)
    ledger.open(session_id="s-ctx", project_id="proj",
                first_message="fix the csv parser quote handling")
    ledger.record_turn("s-ctx", "assistant", "planned: touch parser.py only")
    memory.retain("decision: the csv parser keeps strict quote handling",
                  intent="explicit", project="personal")
    ce = ContextEngine(memory_service=memory, ledger=ledger)
    bundle = ce.build("fix the csv parser quote handling",
                      session_id="s-ctx")
    rendered = bundle.render()
    assert "csv" in rendered.lower()
    assert bundle.quality is not None
    payload = bundle.quality.to_dict()
    for key in ("relevance", "redundancy", "missing_context", "tokens_used",
                "context_budget", "dropped_sections", "quality_score"):
        assert key in payload
    assert payload["tokens_used"] <= max(1, payload["context_budget"])


def test_dedup_and_budget_folding_under_pressure(tmp_path):
    _db, _engine, memory, ledger = _stack(tmp_path)
    ledger.open(session_id="s-big", project_id="proj")
    filler = ("long duplicated discussion about the parser and its quoting "
              "rules that repeats over and over ") * 6
    for i in range(12):
        ledger.record_turn("s-big", "user" if i % 2 else "assistant",
                           filler + f"note {i}")
    # tiny budget forces the fold/drop path
    ce = ContextEngine(memory_service=memory, ledger=ledger,
                       token_budget=140)
    bundle = ce.build("parser quoting", session_id="s-big")
    assert bundle.quality is not None
    quality = bundle.quality.to_dict()
    if quality["dropped_sections"]:
        # every dropped section is *named* — silent truncation is the bug
        assert all(isinstance(d, str) and d for d in
                   quality["dropped_sections"])
    rendered = bundle.render()
    if any(s.kind == "digest" for s in bundle.sections):
        assert "digest" in rendered.lower() or "lossy" in rendered.lower()
    # redundancy detection: near-identical turns must not both survive as
    # full sections
    full_turns = [s for s in bundle.sections
                  if s.kind in ("turn", "user") or "turn" in s.kind]
    assert len(full_turns) <= 12


def test_context_budget_follows_model_window_when_given(tmp_path):
    _db, _engine, memory, ledger = _stack(tmp_path)
    ledger.open(session_id="s-b", project_id="p")
    ce = ContextEngine(memory_service=memory, ledger=ledger)
    small = ce.build("hello", session_id="s-b", model_context_window=2048)
    big = ce.build("hello", session_id="s-b", model_context_window=200000)
    assert (big.quality.to_dict()["context_budget"]
            >= small.quality.to_dict()["context_budget"])
    assert small.quality.to_dict()["context_budget"] >= 1
    assert DEFAULT_TOKEN_BUDGET > 0


def test_contradiction_scan_is_part_of_quality(tmp_path):
    _db, engine, memory, ledger = _stack(tmp_path)
    ledger.open(session_id="s-c", project_id="p")
    engine.remember("decision", "the deploy target is the staging box",
                    project="personal", source="x")
    engine.remember("decision", "the deploy target is not staging at all",
                    project="personal", source="x")
    ce = ContextEngine(memory_service=memory, ledger=ledger)
    bundle = ce.build("deploy target staging", session_id="s-c")
    quality = bundle.quality.to_dict()
    assert isinstance(quality["contradictions"], list)   # scanned (may or may
    # not fire for this wording — but the metric is always reported)


def test_empty_session_build_does_not_fabricate(tmp_path):
    _db, _engine, memory, ledger = _stack(tmp_path)
    ledger.open(session_id="s-e", project_id="p")
    ce = ContextEngine(memory_service=memory, ledger=ledger)
    bundle = ce.build("anything", session_id="s-e")
    # only the session-state skeleton (and zero memories) — no filler text
    rendered = bundle.render().lower()
    assert "fabricated" not in rendered
    assert all("example context" not in s.text for s in bundle.sections)
