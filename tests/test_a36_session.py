"""Voice session orchestration (A36): the permission-gated spoken loop.

One loop: wake → transcribe → parse → A33 policy → approval → action →
spoken reply. Voice is just another request source: every path here
proves it can never bypass the permission system.
"""
from __future__ import annotations

import pytest

from forge.voice import VoiceInterface, VoiceSession
from forge.voice.audio import AudioChunk
from forge.voice.codec import speech_chunk, wake_chunk
from forge.security.approvals import ApprovalStore
from forge.security.audit import AuditLog
from forge.security.policy import PermissionPolicy, PermissionRule, Resource


def voice_policy(run_tests="ALLOW", status="ALLOW") -> PermissionPolicy:
    rules = []
    for scope, effect in (("run_tests", run_tests), ("status", status)):
        rules.append(PermissionRule(
            id=f"voice-{scope}", resource=Resource.VOICE,
            operation="command", scope=scope, effect=effect))
    return PermissionPolicy(rules=rules)


def make_session(**policy_overrides) -> tuple[VoiceSession, ApprovalStore]:
    store = ApprovalStore()
    interface = VoiceInterface(policy=voice_policy(**policy_overrides),
                               store=store, audit=AuditLog())
    return VoiceSession(interface), store


def task_factory(intent):
    return {"kind": "task", "id": f"task-{intent.name}", "status": "QUEUED"}


def concatenate(*chunks: AudioChunk) -> AudioChunk:
    data = b"".join(chunk.data for chunk in chunks)
    return AudioChunk(data=data)


def test_capabilities_label_simulation_honestly():
    session, _store = make_session()
    caps = session.capabilities()
    assert caps["simulation"] is True
    assert caps["transcriber"]["simulation"] is True
    assert caps["synthesizer"]["simulation"] is True
    assert caps["wake"]["word"] == "forge"


def test_typed_command_allow_executes_and_speaks():
    session, _store = make_session()
    result = session.process("run the tests", agent="forge-voice",
                             task_id="t1", task_factory=task_factory)
    assert result.ok
    assert result.transcription["text"] == "run the tests"
    assert result.transcription["engine"] == "forge-typed-input"
    assert result.intent["name"] == "run_tests"
    assert result.permission["decision"] == "ALLOW"
    assert result.action["id"] == "task-run_tests"
    assert result.reply_text == "Done. Task created."
    assert result.response_audio is not None
    assert result.response_audio.wav()[:4] == b"RIFF"
    assert result.response_simulation is True
    payload = result.to_dict()
    assert payload["simulation"] is True
    assert "audio_b64" not in payload["response_audio"]  # raw audio out


def test_status_intent_speaks_informational_reply():
    session, _store = make_session()
    result = session.process("check status", agent="forge-voice",
                             task_factory=lambda intent: {
                                 "kind": "reply", "text": "All quiet."})
    assert result.ok
    assert result.action["kind"] == "reply"
    assert result.reply_text == "All quiet."


def test_approval_required_round_trip_with_single_use_token():
    session, store = make_session(run_tests="REQUIRE_APPROVAL")
    first = session.process("run the tests", agent="forge-voice",
                            task_id="t1", task_factory=task_factory)
    assert not first.ok
    assert first.permission["decision"] == "REQUIRE_APPROVAL"
    assert first.permission["approval_required"]
    approval_id = first.permission["approval_request_id"]
    pending = store.pending()
    assert any(request.id == approval_id for request in pending)
    # A human decides; the A33 store enforces approver != agent.
    store.decide(approval_id, True, decided_by="human")
    token = store.issue(approval_id, decided_by="human", max_uses=1)
    executed = session.process("run the tests", agent="forge-voice",
                               task_id="t1", task_factory=task_factory,
                               approval_token_id=token.id)
    assert executed.ok and executed.action["id"] == "task-run_tests"
    # Replay: the single-use token fails closed into a fresh approval.
    replay = session.process("run the tests", agent="forge-voice",
                             task_id="t1", task_factory=task_factory,
                             approval_token_id=token.id)
    assert not replay.ok
    assert replay.permission["approval_required"]
    assert replay.permission["approval_request_id"] != approval_id


def test_denied_policy_never_creates_action():
    session, store = make_session(run_tests="DENY")
    result = session.process("run the tests", agent="forge-voice",
                             task_id="t1", task_factory=task_factory)
    assert not result.ok
    assert result.permission["decision"] == "DENY"
    assert result.action is None
    assert not store.pending()  # nothing to approve either


def test_unknown_intent_fails_closed():
    session, _store = make_session()
    result = session.process("launch the missiles", agent="forge-voice")
    assert not result.ok
    assert result.intent["name"] == "unknown"
    assert result.action is None


def test_audio_loop_wake_transcribe_act():
    session, _store = make_session()
    audio = concatenate(wake_chunk(), speech_chunk("check status"))
    result = session.process(audio, agent="forge-voice",
                             task_factory=lambda intent: {
                                 "kind": "reply", "text": "All quiet."})
    assert result.ok
    assert result.stages[0]["stage"] == "wake"
    assert result.stages[0]["detail"]["detected"] is True
    assert result.transcription["text"] == "check status"
    assert result.transcription["engine"] == "forge-simulated-stt"


def test_audio_without_wake_fails_closed():
    session, _store = make_session()
    result = session.process(speech_chunk("check status"),
                             agent="forge-voice")
    assert not result.ok
    assert result.error["kind"] == "no_wake_word"
    assert result.action is None


def test_wake_bypass_must_be_explicit():
    session, _store = make_session()
    result = session.process(speech_chunk("check status"),
                             agent="forge-voice", require_wake=False,
                             task_factory=lambda intent: {
                                 "kind": "reply", "text": "All quiet."})
    assert result.ok
    assert result.stages[1]["detail"] == "wake bypass requested explicitly"


def test_arbitrary_audio_refused_at_transcription():
    import random
    import struct
    random.seed(5)
    noise = struct.pack("<8000h", *[random.randint(-3000, 3000)
                                    for _ in range(8000)])
    session, _store = make_session()
    # Wake first, then the recognizer refuses the non-simulated speech.
    audio = concatenate(wake_chunk(), AudioChunk(data=noise))
    result = session.process(audio, agent="forge-voice")
    assert not result.ok
    assert result.error["kind"] == "unrecognized"
    assert result.action is None


def test_denied_loop_still_speaks_structured_reply():
    session, _store = make_session(run_tests="DENY")
    result = session.process("run the tests", agent="forge-voice",
                             task_factory=task_factory)
    assert not result.ok
    assert result.reply_text  # the denial reason is spoken back
    assert result.response_audio is not None


def test_permission_evaluations_are_audited():
    audit = AuditLog()
    store = ApprovalStore()
    interface = VoiceInterface(policy=voice_policy(), store=store,
                               audit=audit)
    session = VoiceSession(interface)
    session.process("run the tests", agent="forge-voice",
                    task_factory=task_factory)
    assert any(event.operation == "command" and event.resource == "voice"
               for event in audit.events)
