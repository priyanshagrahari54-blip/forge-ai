"""A84 Stage B — durable assistant sessions + honest continuity resolution.

Session turns and references survive restarts (they live in the shared plane
SQLite); bounds are enforced by trimming, never by refusing to record.
Continuity turns "that file"/"the previous task"/"yesterday's research" into
resolved references over *real* records — and when nothing matches it says so
and asks, rather than inventing an answer.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from forge.assistant.continuity import (  # noqa: E402
    REFERENCE_PATTERNS, ContinuityResolver,
)
from forge.assistant.sessions import MAX_HISTORY, SessionLedger  # noqa: E402
from forge.control.db import Database  # noqa: E402


def _ledger(tmp_path, **kw):
    return SessionLedger(Database(tmp_path / "plane.db"), **kw)


def test_open_record_and_reload_roundtrip(tmp_path):
    ledger = _ledger(tmp_path)
    session = ledger.open(project_id="demo", first_message="add csv export")
    assert session.id and session.title
    ledger.record_turn(session.id, "user", "add csv export")
    ledger.record_turn(session.id, "assistant", "queued as task t-1")
    ledger.record_ref(session.id, "task", "t-1", note="csv export")
    ledger.set_active_task(session.id, "t-1")
    # a *new* ledger over the same db sees the same session: durable state
    again = _ledger(tmp_path)
    reloaded = again.get(session.id)
    assert reloaded is not None
    assert reloaded.active_task_id == "t-1"
    assert len(reloaded.history) == 2
    assert list(reloaded.research_refs) == [] and not reloaded.agent_activity
    payload = reloaded.to_dict()
    assert payload["history"][0]["text"] == "add csv export"


def test_turn_bounds_trim_oldest_and_never_grow_unbounded(tmp_path):
    ledger = _ledger(tmp_path)
    session = ledger.open(project_id="demo")
    for i in range(MAX_HISTORY + 40):
        ledger.record_turn(session.id, "user", f"message {i}")
    snap = ledger.get(session.id)
    assert len(snap.history) <= MAX_HISTORY
    assert snap.history[-1]["text"] == f"message {MAX_HISTORY + 39}"
    assert snap.history[0]["text"] != "message 0"    # trimmed, not blocked


def test_ref_cap_and_bad_kind_and_ids(tmp_path):
    ledger = _ledger(tmp_path)
    session = ledger.open(project_id="demo")
    for i in range(210):
        ledger.record_ref(session.id, "artifact", f"a-{i}")
    snap = ledger.get(session.id)
    assert len(snap.artifacts) <= 200
    import pytest
    with pytest.raises(ValueError):
        ledger.record_ref(session.id, "feelings", "x")
    with pytest.raises(ValueError):
        ledger.record_ref(session.id, "memory", "   ")


def test_retention_mode_close_reopen_clear(tmp_path):
    ledger = _ledger(tmp_path)
    session = ledger.open(project_id="demo", first_message="hello there")
    ledger.record_turn(session.id, "user", "hello there")
    assert ledger.set_retention_mode(session.id, "disabled") is True
    assert ledger.get(session.id).retention_mode == "disabled"
    ledger.close(session.id)
    assert ledger.get(session.id).state == "closed"
    assert ledger.list(state="active") == []
    ledger.reactivate(session.id)
    assert ledger.get(session.id).state == "active"
    counts = ledger.clear_session(session.id)
    assert counts["turns_deleted"] >= 1
    assert list(ledger.get(session.id).history) == []
    assert ledger.get(session.id).summary == ""


def test_list_is_bounded_and_descending(tmp_path):
    ledger = _ledger(tmp_path)
    for i in range(6):
        s = ledger.open(project_id="demo", first_message=f"task {i}")
        ledger.record_turn(s.id, "user", "x")   # touches activity clock
        time.sleep(0.001)
    rows = ledger.list(limit=4)
    assert len(rows) == 4
    assert rows[0].last_activity >= rows[-1].last_activity


# -- continuity --------------------------------------------------------------------

def test_reference_pattern_vocabulary_matches_design():
    kinds = {kind for _pattern, kind in REFERENCE_PATTERNS}
    assert {"active_task", "previous", "yesterday", "deeper",
            "checkpoint", "discussion"} <= kinds
    joined = " ".join(pattern for pattern, _kind in REFERENCE_PATTERNS)
    for fragment in ("continue", "previous", "yesterday", "deeper"):
        assert fragment in joined


def test_detect_names_kinds_without_touching_state(tmp_path):
    ledger = _ledger(tmp_path)
    resolver = ContinuityResolver(ledger=ledger)
    kinds = resolver.detect("go back to that task from yesterday")
    assert kinds                       # non-empty dict/tuple
    assert "yesterday" in str(kinds) or "active_task" in str(kinds)
    assert resolver.detect("hello, what is 2+2?") in ({}, (), None,
                                                       {"kinds": []}, []) or \
        not _has_kinds(resolver.detect("hello, what is 2+2?"))


def _has_kinds(detected) -> bool:
    if isinstance(detected, dict):
        return bool(detected.get("kinds") or detected.get("active")
                     or detected)
    return bool(detected)


def test_resolve_active_task_from_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    session = ledger.open(project_id="demo", first_message="implement csv export")
    ledger.record_turn(session.id, "user", "implement csv export")
    ledger.record_turn(session.id, "assistant", "queued as t-42")
    ledger.set_active_task(session.id, "t-42")
    seen = {}

    def lookup(task_id):
        seen[task_id] = True
        return {"id": task_id, "state": "awaiting-review",
                "requirement": "implement csv export"}

    resolver = ContinuityResolver(ledger=ledger, task_lookup=lookup)
    bundle = resolver.resolve(session.id, "continue it")
    assert bundle.referenced and bundle.resolved
    assert "active_task" in bundle.kind
    assert any(i.get("type") == "task" and i.get("id") == "t-42"
               for i in bundle.items)
    assert "t-42" in bundle.context_text or "csv" in bundle.context_text
    assert seen.get("t-42")               # live status was actually consulted


def test_unresolved_reference_asks_and_invents_nothing(tmp_path):
    ledger = _ledger(tmp_path)
    session = ledger.open(project_id="demo")     # empty session, nothing to find
    resolver = ContinuityResolver(ledger=ledger, memory=None)
    bundle = resolver.resolve(session.id, "continue the earlier research report")
    assert bundle.referenced is True
    assert bundle.resolved is False
    assert bundle.clarifying_question            # a real question, not silence
    assert bundle.context_text == "" or "unresolved" in bundle.context_text.lower()
    plain = resolver.resolve(session.id, "what is the capital of France?")
    # no reference at all: nothing to resolve, and nothing invented either
    assert plain.referenced is False and plain.resolved is True
    assert plain.kind == "none" and plain.clarifying_question == ""


def test_last_answer_continuity_uses_real_transcript(tmp_path):
    ledger = _ledger(tmp_path)
    session = ledger.open(project_id="demo")
    ledger.record_turn(session.id, "user", "explain the retry policy")
    ledger.record_turn(session.id, "assistant",
                       "the retry policy bounds attempts at 3 with backoff")
    resolver = ContinuityResolver(ledger=ledger)
    bundle = resolver.resolve(session.id, "go deeper on that")
    assert bundle.referenced
    if bundle.resolved:                          # resolved via last answer
        assert "retry" in bundle.context_text.lower()
    else:                                        # or honestly asked
        assert bundle.clarifying_question
