"""A81 structured agent communication and activity model.

Agents communicate only through validated messages (typed, bounded,
addressed, auditable) — never uncontrolled shared state — and the
activity tracker folds the scheduler event stream into the per-role
snapshot the desktop views render.
"""
from __future__ import annotations

import pytest

from forge.orchestration.activity import AgentActivityTracker
from forge.orchestration.messages import (MAX_CONTENT, AgentMessage,
                                          MessageBus)


def test_message_contract_rejects_bad_fields():
    bus = MessageBus()
    with pytest.raises(ValueError, match="Unknown message type"):
        bus.send("a", "b", "t1", "telepathy")
    with pytest.raises(ValueError, match="receiver"):
        bus.send("a", "", "t1", "query")
    with pytest.raises(ValueError, match="content"):
        bus.send("a", "b", "t1", "query", content="x" * (MAX_CONTENT + 1))
    with pytest.raises(ValueError, match="Confidence"):
        bus.send("a", "b", "t1", "query", confidence=1.5)
    with pytest.raises(ValueError, match="another role"):
        bus.send("a", "a", "t1", "query")


def test_messages_are_addressed_and_ordered():
    bus = MessageBus()
    m1 = bus.send("coder", "reviewer", "t2", "task_result",
                  content='{"files": ["a.py"]}',
                  evidence=("from:t1",), confidence=1.0)
    m2 = bus.send("supervisor", "reviewer", "t2", "task_request",
                  content="please review")
    assert isinstance(m1, AgentMessage)
    inbox = bus.inbox("reviewer")
    assert [m.message_type for m in inbox] == ["task_result", "task_request"]
    # inbox is drained
    assert bus.inbox("reviewer") == []
    # the full log survives
    assert len(bus.log()) == 2
    # per-task view
    assert len(bus.for_task("t2")) == 2


def test_unknown_receiver_is_an_error_not_a_drop():
    bus = MessageBus(roles={"coder", "tester"})
    bus.send("coder", "tester", "t1", "handoff", content="x")
    with pytest.raises(ValueError, match="Unknown message receiver"):
        bus.send("coder", "ghost", "t1", "handoff")


def test_message_roundtrip_through_dicts():
    bus = MessageBus()
    bus.send("coder", "tester", "t1", "task_result", content="ok",
             evidence=("a", "b"), confidence=0.9)
    payload = bus.to_list()[0]
    restored = AgentMessage.from_dict(payload)
    assert restored.to_dict() == payload


def _events_to_states():
    events = [
        ("run_started", {"run_id": "r1", "requirement": "demo"}),
        ("task_queued", {"task_id": "t1", "role": "coder"}),
        ("task_queued", {"task_id": "t2", "role": "tester"}),
        ("lock_acquired", {"task_id": "t1", "role": "coder",
                           "keys": ["a.py"]}),
        ("task_started", {"task_id": "t1", "role": "coder", "attempt": 1}),
        ("message_sent", {"sender": "coder", "receiver": "tester",
                          "task_id": "t2", "type": "task_result",
                          "content": '{"files": ["a.py"]}'}),
        ("task_finished", {"task_id": "t1", "role": "coder",
                           "status": "SUCCEEDED"}),
        ("task_blocked", {"task_id": "t2", "role": "tester",
                          "reason": "dependency t1 ended FAILED"}),
        ("run_finished", {"status": "FAILED"}),
    ]
    tracker = AgentActivityTracker()
    for name, details in events:
        tracker.on_event(name, details)
    return tracker


def test_activity_tracker_folds_events_into_role_states():
    tracker = _events_to_states()
    snap = tracker.snapshot()
    assert snap["status"] == "FAILED"
    assert snap["run_id"] == "r1"
    by_role = {agent["role"]: agent for agent in snap["agents"]}
    assert by_role["coder"]["state"] == "succeeded"
    assert by_role["tester"]["state"] == "blocked"
    assert "dependency t1" in by_role["tester"]["blocked_reason"]
    # structured message is visible in the recent trail
    assert snap["recent_messages"]
    assert snap["recent_messages"][0]["receiver"] == "tester"
    # counters
    assert snap["counts"].get("succeeded") == 1
    assert snap["counts"].get("blocked") == 1


def test_activity_tracker_retries_and_locks():
    tracker = AgentActivityTracker()
    tracker.on_event("task_queued", {"task_id": "t1", "role": "coder"})
    tracker.on_event("lock_wait", {"task_id": "t1", "role": "coder"})
    assert tracker.snapshot()["agents"][0]["state"] == "waiting_lock"
    tracker.on_event("task_started", {"task_id": "t1", "role": "coder",
                                      "attempt": 1})
    tracker.on_event("task_retried", {"task_id": "t1", "role": "coder",
                                      "attempt": 2})
    tracker.on_event("task_finished", {"task_id": "t1", "role": "coder",
                                       "status": "FAILED",
                                       "error": "boom"})
    agent = tracker.snapshot()["agents"][0]
    assert agent["state"] == "failed"
    assert agent["attempts"] == 2
    assert agent["error"] == "boom"
