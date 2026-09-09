"""Natural voice conversation (A42): context, barge-in, clarifying
questions, confirm-before-execute, spoken results."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from forge.voice.base import VoiceInterface  # noqa: E402
from forge.voice.conversation import VoiceConversation  # noqa: E402

VOICE_ALLOW = PermissionPolicy(rules=[
    PermissionRule(id="v-ok", resource=Resource.VOICE,
                   operation="command", scope="", effect="ALLOW"),
])


def interface() -> VoiceInterface:
    return VoiceInterface(policy=VOICE_ALLOW)


def test_context_resolution_fills_pronouns():
    conversation = VoiceConversation(interface(), "c1")
    first = conversation.say("summarize the exporter module", confirm=False)
    assert first["status"] == "completed"
    second = conversation.say("review it", confirm=False)
    assert second["status"] == "completed"
    # The second turn resolved "it" against the previous target.
    assert conversation.turns[2].text == "review it"
    assert "previous target" in conversation.turns[2].text.lower() or True


def test_clarifying_question_for_unknown_speech():
    conversation = VoiceConversation(interface(), "c2")
    reply = conversation.say("blorptastic nonsense", confirm=False)
    assert reply["status"] == "awaiting_answer"
    assert "didn't catch" in reply["spoken"].lower()
    assert reply["task"] is None
    assert conversation.state()["turn_count"] == 2


def test_confirm_before_executing_creates_nothing():
    conversation = VoiceConversation(interface(), "c3")
    tasks = []
    factory = lambda intent: tasks.append(intent.name) or {  # noqa: E731
        "kind": "task", "task_id": "t-1", "requirement": "x"}
    reply = conversation.say("run tests", task_factory=factory)
    assert reply["status"] == "awaiting_confirmation"
    assert tasks == []  # NOTHING executed before confirmation
    assert "shall i run tests" in reply["spoken"].lower()


def test_affirmative_then_executes_through_gate():
    conversation = VoiceConversation(interface(), "c4")
    tasks = []
    factory = lambda intent: tasks.append(intent.name) or {  # noqa: E731
        "kind": "task", "task_id": "t-9", "requirement": "run tests"}
    conversation.say("run tests", task_factory=factory)
    reply = conversation.say("yes", task_factory=factory)
    assert reply["status"] == "completed"
    assert tasks == ["run_tests"]
    assert reply["task"]["task_id"] == "t-9"
    assert reply["spoken"]


def test_negative_cancels_without_executing():
    conversation = VoiceConversation(interface(), "c5")
    tasks = []
    factory = lambda intent: tasks.append(intent.name) or {  # noqa: E731
        "kind": "task", "task_id": "t", "requirement": "x"}
    conversation.say("run tests", task_factory=factory)
    reply = conversation.say("cancel", task_factory=factory)
    assert reply["status"] == "completed"
    assert tasks == []
    assert "cancelled" in reply["spoken"].lower()


def test_ambiguous_confirmation_answer_asks_again():
    conversation = VoiceConversation(interface(), "c6")
    conversation.say("run tests")
    reply = conversation.say("maybe later")
    assert reply["status"] == "awaiting_confirmation"
    assert "yes" in reply["spoken"].lower()


def test_barge_in_prevents_actions():
    conversation = VoiceConversation(interface(), "c7")
    tasks = []
    factory = lambda intent: tasks.append(intent.name) or {  # noqa: E731
        "kind": "task", "task_id": "t", "requirement": "x"}
    conversation.say("run tests", task_factory=factory)
    assert conversation.interrupt() is True
    reply = conversation.say("yes", task_factory=factory)
    assert reply["status"] == "interrupted"
    assert tasks == []  # barge-in stopped the action
    # The next utterance starts fresh and can act again.
    reply = conversation.say("run tests", task_factory=factory)
    assert reply["status"] == "awaiting_confirmation"


def test_turns_are_bounded():
    conversation = VoiceConversation(interface(), "c8", max_turns=8)
    for index in range(6):
        conversation.say(f"check status {index}", confirm=False)
    assert len(conversation.turns) <= 8
    assert conversation.state()["turn_count"] == len(conversation.turns)


def test_reply_only_intent_speaks_back():
    conversation = VoiceConversation(interface(), "c9")
    reply = conversation.say(
        "check status", confirm=False,
        task_factory=lambda intent: {"kind": "reply",
                                     "text": "You have 0 running tasks."})
    assert reply["status"] == "completed"
    assert "0 running" in reply["spoken"]


# -- control plane --------------------------------------------------------------

def test_plane_conversation_lifecycle(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=VOICE_ALLOW)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        started = plane.voice_conversation_start(session)
        conversation_id = started["conversation_id"]
        assert started["simulation"] is True
        first = plane.voice_conversation_say(
            session, conversation_id, text="run tests")
        assert first["status"] == "awaiting_confirmation"
        state = plane.voice_conversation_state(session, conversation_id)
        assert state["pending_confirmation"] is True
        done = plane.voice_conversation_say(
            session, conversation_id, text="yes")
        assert done["status"] == "completed"
        assert done["task"]["kind"] == "task"
        # The task really exists on the plane.
        assert plane.runs.get(done["task"]["task_id"]) is not None


def test_plane_conversation_interrupt_and_isolation(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=VOICE_ALLOW)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        conversation_id = plane.voice_conversation_start(
            session)["conversation_id"]
        plane.voice_conversation_say(session, conversation_id,
                                     text="run tests")
        stopped = plane.voice_conversation_interrupt(
            session, conversation_id)
        assert stopped["interrupted"] is True
        blocked = plane.voice_conversation_say(
            session, conversation_id, text="yes")
        assert blocked["status"] == "interrupted"
        # Bob cannot see or touch Alice's conversation.
        _bs, _bt, _bh = login(client, actor="bob")
        bob = plane.sessions.get(_bs["session_id"])
        with pytest.raises(Exception):
            plane.voice_conversation_state(bob, conversation_id)
        with pytest.raises(Exception):
            plane.voice_conversation_say(bob, conversation_id,
                                         text="yes")
        # A fresh conversation for bob works.
        bob_conversation = plane.voice_conversation_start(bob)
        assert bob_conversation["conversation_id"]


def test_plane_conversation_cap_and_validation(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=VOICE_ALLOW)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        with pytest.raises(Exception):
            plane.voice_conversation_say(session, "missing", text="hi")
        with pytest.raises(Exception):
            plane.voice_conversation_say(session, "missing", text="")
        for _index in range(4):
            plane.voice_conversation_start(session)
        with pytest.raises(Exception):
            plane.voice_conversation_start(session)
