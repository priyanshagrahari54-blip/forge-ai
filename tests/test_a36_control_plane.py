"""Voice control-plane + API integration tests (A36).

The spoken loop through the cockpit API: honest capabilities, synthesis,
transcription (with real-audio refusal), the full process round trip with
approval tokens, wake gating, cross-session isolation, and boundaries.
"""
from __future__ import annotations

import base64
import io
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.voice.codec import speech_chunk, wake_chunk  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def voice_policy(run_tests="REQUIRE_APPROVAL",
                 status="ALLOW") -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="voice-run", resource=Resource.VOICE,
                       operation="command", scope="run_tests",
                       effect=run_tests),
        PermissionRule(id="voice-status", resource=Resource.VOICE,
                       operation="command", scope="status",
                       effect=status),
    ])


def _setup(tmp_path):
    plane = make_plane(tmp_path, start=False, policy=voice_policy())
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def _headers(client, profile="assisted"):
    with client:
        _session, _token, headers = login(client, profile=profile)
    return headers


def _join_wavs(*chunks) -> str:
    """Concatenate PCM chunks into one WAV and return its base64."""
    data = b"".join(chunk.data for chunk in chunks)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(chunks[0].channels)
        out.setsampwidth(chunks[0].sample_width)
        out.setframerate(chunks[0].sample_rate)
        out.writeframes(data)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_capabilities_are_honest(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        caps = client.get("/api/v1/voice/capabilities",
                          headers=headers).json()
        assert caps["status"] == "simulation"
        assert caps["simulation"] is True
        assert "simulated" in caps["transcriber"]["name"]
        assert "simulated" in caps["synthesizer"]["name"]
        assert caps["wake"]["word"] == "forge"
        assert "real" in caps["note"].lower()


def test_synthesize_and_playback_round_trip(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        payload = client.post("/api/v1/voice/synthesize", headers=headers,
                              json={"text": "check status"}).json()
        assert payload["mime"] == "audio/wav"
        assert payload["simulation"] is True
        assert payload["engine"] == "forge-simulated-tts"
        assert payload["duration_ms"] > 0
        raw = base64.b64decode(payload["audio_b64"])
        assert raw[:4] == b"RIFF"
        # The synthesized utterance round-trips through transcription.
        transcript = client.post("/api/v1/voice/transcribe", headers=headers,
                                 json={"audio_b64":
                                       payload["audio_b64"]}).json()
        assert transcript["transcription"]["text"] == "check status"
        assert transcript["transcription"]["simulation"] is True


def test_transcribe_refuses_real_audio_and_garbage(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        # Valid WAV, arbitrary content: refused honestly, never guessed.
        import random
        import struct
        random.seed(9)
        noise_pcm = struct.pack(
            "<8000h", *[random.randint(-3000, 3000)
                        for _ in range(8000)])
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(noise_pcm)
        noise = base64.b64encode(buffer.getvalue()).decode("ascii")
        refused = client.post("/api/v1/voice/transcribe", headers=headers,
                              json={"audio_b64": noise})
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "VOICE_UNAVAILABLE"
        # Not even a WAV: invalid request.
        garbage = client.post(
            "/api/v1/voice/transcribe", headers=headers,
            json={"audio_b64": base64.b64encode(b"nope").decode()})
        assert garbage.status_code == 400


def test_audio_process_requires_wake_word(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        audio = _join_wavs(speech_chunk("check status"))
        result = client.post("/api/v1/voice/process", headers=headers,
                             json={"audio_b64": audio}).json()
        assert result["ok"] is False
        assert result["error"]["kind"] == "no_wake_word"
        # With the wake marker: the full spoken loop completes.
        audio = _join_wavs(wake_chunk(), speech_chunk("check status"))
        result = client.post("/api/v1/voice/process", headers=headers,
                             json={"audio_b64": audio}).json()
        assert result["ok"] is True
        assert result["input_mode"] == "audio"
        assert result["transcription"]["text"] == "check status"
        assert result["stages"][0]["stage"] == "wake"
        assert result["intent"]["name"] == "status"
        assert result["action"]["kind"] == "reply"
        assert "running" in result["reply_text"]
        assert result["response_audio"]["simulation"] is True


def test_process_approval_round_trip_and_token_single_use(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        # run tests -> policy REQUIRE_APPROVAL: nothing executes yet.
        first = client.post("/api/v1/voice/process", headers=headers,
                            json={"text": "run the tests"}).json()
        assert first["ok"] is False
        assert first["permission"]["decision"] == "REQUIRE_APPROVAL"
        approval_id = first["permission"]["approval_request_id"]
        # Visible to this session's voice approvals.
        visible = client.get("/api/v1/voice/approvals",
                             headers=headers).json()["approvals"]
        assert any(item["id"] == approval_id for item in visible)
        # The generic run-bound approvals view does NOT list voice
        # approvals (they are session-bound).
        generic = client.get("/api/v1/approvals",
                             headers=headers).json()["approvals"]
        assert all(item["id"] != approval_id for item in generic)
        # Approve -> single-use token.
        decided = client.post(
            f"/api/v1/voice/approvals/{approval_id}/approve",
            headers=headers, json={}).json()
        assert decided["token_id"]
        # Resubmit with the token: the task is created for real.
        executed = client.post("/api/v1/voice/process", headers=headers,
                               json={"text": "run the tests",
                                     "approval_id": decided["token_id"]})
        assert executed.status_code == 200
        body = executed.json()
        assert body["ok"] is True
        assert body["action"]["kind"] == "task"
        assert body["action"]["status"] == "QUEUED"
        # Replay: single-use token fails closed into a fresh approval.
        replay = client.post("/api/v1/voice/process", headers=headers,
                             json={"text": "run the tests",
                                   "approval_id": decided["token_id"]}).json()
        assert replay["ok"] is False
        assert replay["permission"]["approval_required"]
        assert replay["permission"]["approval_request_id"] != approval_id


def test_voice_approval_deny_round_trip(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        first = client.post("/api/v1/voice/process", headers=headers,
                            json={"text": "run the tests"}).json()
        approval_id = first["permission"]["approval_request_id"]
        denied = client.post(
            f"/api/v1/voice/approvals/{approval_id}/deny",
            headers=headers, json={}).json()
        assert denied["approval"]["status"] == "denied"
        retry = client.post("/api/v1/voice/process", headers=headers,
                            json={"text": "run the tests"}).json()
        assert retry["ok"] is False


def test_cross_session_isolation_of_voice_approvals(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        first = client.post("/api/v1/voice/process", headers=headers,
                            json={"text": "run the tests"}).json()
        approval_id = first["permission"]["approval_request_id"]
        # Bob cannot see or decide Alice's voice approval.
        _session, _token, bob = login(client, actor="bob")
        visible = client.get("/api/v1/voice/approvals",
                             headers=bob).json()["approvals"]
        assert all(item["id"] != approval_id for item in visible)
        decided = client.post(
            f"/api/v1/voice/approvals/{approval_id}/approve",
            headers=bob, json={})
        assert decided.status_code == 404


def test_voice_endpoints_require_session_and_bounds(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        for method, path in (("get", "/api/v1/voice/capabilities"),
                             ("get", "/api/v1/voice/approvals")):
            assert getattr(client, method)(path).status_code == 401
        for path in ("/api/v1/voice/process", "/api/v1/voice/synthesize",
                     "/api/v1/voice/transcribe"):
            assert client.post(path, json={}).status_code == 401
        headers = _headers(client)
        oversized = client.post("/api/v1/voice/process", headers=headers,
                                json={"text": "x" * 2001})
        assert oversized.status_code == 400
        both = client.post("/api/v1/voice/process", headers=headers,
                           json={"text": "check status",
                                 "audio_b64": "AAAA"})
        assert both.status_code == 400
        empty = client.post("/api/v1/voice/process", headers=headers,
                            json={})
        assert empty.status_code == 400


def test_denied_policy_never_creates_tasks(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        # A policy that denies voice command entirely.
        plane.policy = PermissionPolicy(rules=[
            PermissionRule(id="voice-deny", resource=Resource.VOICE,
                           operation="command", scope="run_tests",
                           effect="DENY"),
        ])
        headers = _headers(client)
        result = client.post("/api/v1/voice/process", headers=headers,
                             json={"text": "run the tests"}).json()
        assert result["ok"] is False
        assert result["permission"]["decision"] == "DENY"
        assert result["action"] is None
        assert result["error"] is None


def test_voice_actions_are_audited(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        headers = _headers(client)
        client.post("/api/v1/voice/process", headers=headers,
                    json={"text": "check status"})
        entries = plane.audit.events
        assert any(event.resource == "voice" and event.operation == "process"
                   for event in entries)
