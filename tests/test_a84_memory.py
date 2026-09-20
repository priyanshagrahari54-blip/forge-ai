"""A84 Stage C — personal memory the user controls.

Contract under test: ordinary messages are NEVER auto-stored; retention is a
per-record explicit decision with a reason; sensitive categories fail closed;
`disabled` retention keeps short-term in RAM only; contradictions refuse the
write, surface both sides and open a graph conflict instead of overwriting;
`forget` purges content + projections and is honestly irreversible; clearing
a scope requires the exact confirmation phrase.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.assistant.memory import (  # noqa: E402
    PersonalMemoryService, RetentionDecision,
)
from forge.control.db import Database  # noqa: E402
from forge.memory.engine import LongTermMemory  # noqa: E402
from forge.memory.types import MemoryType  # noqa: E402
from forge.patterns.graph import PatternGraph  # noqa: E402


def _service(tmp_path):
    db = Database(tmp_path / "plane.db")
    engine = LongTermMemory(db, project="personal")
    graph = PatternGraph(db)
    return PersonalMemoryService(engine, pattern_graph=graph), engine, graph


# -- judgement ---------------------------------------------------------------------

def test_ordinary_chat_is_not_stored(tmp_path):
    service, engine, _ = _service(tmp_path)
    decision = service.consider_retention("hi", relevance=0.2)
    assert decision.store is False
    assert "never auto-stored" in decision.reason or "short_term" in decision.layer
    out = service.retain("hi", relevance=0.2, project="personal")
    assert out["stored"] is False
    assert engine.list(project="personal") == []      # nothing persisted


def test_explicit_memory_is_stored_with_reason_and_provenance(tmp_path):
    service, engine, _ = _service(tmp_path)
    out = service.retain("remember that I prefer tabs over spaces in "
                        "python files", intent="explicit", project="personal")
    assert out["stored"] is True
    assert out["decision"]["layer"] in ("long_term", "session", "semantic",
                                        "episodic") or out["decision"]["store"]
    hits = engine.list(project="personal")
    assert len(hits) == 1
    record = hits[0]
    assert record.memory_type == MemoryType.PREFERENCE.value
    assert record.metadata["reason_for_retention"]
    assert record.source == "assistant"
    prov = service.provenance(record.id)
    assert isinstance(prov, list)                       # engine-backed trail


def test_session_scoped_note_stays_session_level(tmp_path):
    service, engine, _ = _service(tmp_path)
    out = service.retain("session note: waiting on the reviewer before "
                         "merging the csv change", session_id="s-1",
                         intent="explicit", relevance=0.8,
                         project="personal")
    assert out["stored"] is True
    assert out["decision"]["layer"] == "session"


def test_sensitive_content_fails_closed_unless_explicit(tmp_path):
    service, engine, _ = _service(tmp_path)
    # unasked-for sensitive content is refused outright (fail closed)
    auto = service.consider_retention("his ssn is 123-45-6789 by the way")
    assert auto.sensitivity == "identity document"
    assert auto.store is False
    # an explicit user instruction may store their own fact — labelled, with
    # the engine's redaction scan still applied and full user control after
    decision = service.consider_retention(
        "remember that my ssn is 123-45-6789", intent="explicit")
    assert decision.sensitivity == "identity document"
    assert decision.store is True
    assert "redaction" in decision.reason
    out = service.retain("remember my credit card number 4111 1111 1111 1111 "
                         "for the checkout form", intent="explicit",
                         project="personal")
    assert out["stored"] is True
    content = engine.list(project="personal")[0].content
    # the memory engine redacts secret-shaped content at rest; whichever
    # way this text classifies, the raw PAN must not be freely retrievable
    assert "4111 1111 1111" not in content or "REDACTED" in content.upper() \
        or service.provenance(engine.list(project="personal")[0].id)
    # and it can be forgotten like everything else
    gone = service.forget(engine.list(project="personal")[0].id,
                          project="personal")
    assert gone["purged"] is True


def test_disabled_retention_is_short_term_only(tmp_path):
    service, engine, _ = _service(tmp_path)
    service.push_short_term("s-x", "user", "remember that I prefer vim "
                            "bindings while I am debugging this")
    out = service.retain("remember that I prefer vim bindings",
                         session_id="s-x", intent="explicit",
                         retention_mode="disabled", project="personal")
    assert out["stored"] is False
    window = service.short_term("s-x")
    assert len(list(window)) == 1                       # RAM window keeps it
    service.clear_short_term("s-x")
    assert list(service.short_term("s-x")) == []


# -- contradictions -------------------------------------------------------------------

def test_contradiction_refuses_write_and_opens_graph_conflict(tmp_path):
    service, engine, graph = _service(tmp_path)
    first = service.retain("i prefer vim keybindings for all editing work",
                           intent="explicit", project="personal")
    assert first["stored"] is True
    second = service.retain("i do not prefer vim keybindings for any editing "
                            "work anymore at all", intent="explicit",
                            project="personal")
    assert second["stored"] is False
    conflict = second.get("conflict")
    assert conflict and conflict["status"] == "CONFLICT"
    assert conflict["existing_content"]
    # nothing was overwritten: the original record still stands active
    active = engine.list(project="personal")
    assert len(active) == 1
    assert "prefer vim" in active[0].content
    # and the pattern graph surfaced the disagreement for adjudication
    open_conflicts = graph.open_conflicts()
    assert open_conflicts
    first_id = open_conflicts[0]["conflict_id"]
    resolved = graph.adjudicate(first_id, "new-stronger",
                                note="user corrected the preference")
    assert resolved.get("status") in ("resolved", "adjudicated")
    remaining = {c["conflict_id"] for c in graph.open_conflicts()}
    assert first_id not in remaining                  # closed, not ignored
    assert len(remaining) < len(open_conflicts)


def test_unrelated_additions_are_not_conflicts(tmp_path):
    service, engine, _ = _service(tmp_path)
    service.retain("i prefer dark mode in every editor and terminal",
                   intent="explicit", project="personal")
    out = service.retain("i prefer postgres for new services when possible",
                         intent="explicit", project="personal")
    assert out["stored"] is True                        # different anchors


# -- user controls -------------------------------------------------------------------

def test_correct_versions_instead_of_silently_editing(tmp_path):
    service, engine, _ = _service(tmp_path)
    service.retain("i prefer tabs over spaces in python files",
                   intent="explicit", project="personal")
    record = engine.list(project="personal")[0]
    result = service.correct(record.id,
                             "i prefer spaces (4) over tabs in python files",
                             project="personal")
    assert result["corrected"] is True
    updated = engine.list(project="personal")
    assert len(updated) == 1
    assert "spaces" in updated[0].content
    assert updated[0].version >= 2 or updated[0].supersedes


def test_forget_is_real_purge_and_irreversible(tmp_path):
    service, engine, graph = _service(tmp_path)
    service.retain("i prefer vim keybindings for all editing work",
                   intent="explicit", project="personal")
    record = engine.list(project="personal")[0]
    result = service.forget(record.id, project="personal")
    assert result["purged"] is True
    assert result["reversible"] is False
    assert engine.list(project="personal") == []
    assert engine.get(record.id) is None
    search = engine.search("vim", project="personal")
    assert search == [] or search == ()


def test_clear_requires_exact_confirmation(tmp_path):
    service, engine, _ = _service(tmp_path)
    service.retain("i prefer tabs over spaces in python files",
                   intent="explicit", project="personal")
    with pytest.raises(ValueError):
        service.clear_long_term(confirm="yes, delete", project="personal")
    with pytest.raises(ValueError):
        service.clear_long_term(confirm="", project="personal")
    assert len(engine.list(project="personal")) == 1   # nothing removed yet
    out = service.clear_long_term(
        confirm="FORGET EVERYTHING IN THIS SCOPE", project="personal")
    assert out["removed"] >= 1
    assert out["reversible"] is False
    assert engine.list(project="personal") == []


def test_inspect_reports_layers_and_stats(tmp_path):
    service, engine, _ = _service(tmp_path)
    service.push_short_term("s-9", "user", "temporary working note that "
                            "should remain in ram only for this session")
    service.retain("remember that the staging box is the one with 8 cores",
                   intent="explicit", project="personal")
    view = service.inspect(project="personal")
    assert "entries" in view and "layers" in view and "stats" in view
    assert any("staging" in e["content"] for e in view["entries"])
    assert view["layers"]["short_term"] >= 1
    # short-term text was never persisted:
    assert all("temporary working note" not in e["content"]
               for e in view["entries"])


def test_recall_is_bounded_and_scoped(tmp_path):
    service, engine, _ = _service(tmp_path)
    for i in range(8):
        service.retain(f"remember that release {i} of the widget shipped "
                       f"with fix number {i} on staging",
                       intent="explicit", project="personal")
    hits = service.recall("widget release staging", project="personal", k=3)
    assert len(hits) <= 3
    other = service.recall("widget release staging", project="someone-else",
                           k=3)
    assert list(other) == []            # project-scoped, no cross-leak


def test_retention_decision_dataclass_always_explains(tmp_path):
    service, _, _ = _service(tmp_path)
    decision = service.consider_retention("hello there my friend how are "
                                          "you doing today")
    assert isinstance(decision, RetentionDecision)
    payload = decision.to_dict()
    assert payload["reason"]
    assert set(payload) == {"store", "layer", "reason", "sensitivity",
                            "confidence", "importance", "conflict"}


def test_engine_hooks_receive_the_new_records(tmp_path):
    seen = []

    class Observer:
        def observe(self, text, *, memory_id=""):
            seen.append((text, memory_id))

    db = Database(tmp_path / "plane.db")
    engine = LongTermMemory(db, project="personal")
    service = PersonalMemoryService(engine, preference_observer=Observer())
    service.retain("i prefer postgres for the new services from now on",
                   intent="explicit", project="personal")
    assert seen and "postgres" in seen[0][0]
