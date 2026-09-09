"""Voice conversation API tests (A42)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)

VOICE_ALLOW = PermissionPolicy(rules=[
    PermissionRule(id="v-ok", resource=Resource.VOICE,
                   operation="command", scope="", effect="ALLOW"),
])


def _setup(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=VOICE_ALLOW)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def test_conversation_flow_over_http(tmp_path):
    _plane, client = _setup(tmp_path)
    with client:
        _session, _token, headers = login(client)
        started = client.post("/api/v1/voice/conversations",
                              headers=headers, json={})
        assert started.status_code == 200
        conversation_id = started.json()["conversation_id"]
        first = client.post(
            f"/api/v1/voice/conversations/{conversation_id}/say",
            headers=headers, json={"text": "run tests"})
        assert first.status_code == 200
        assert first.json()["status"] == "awaiting_confirmation"
        state = client.get(
            f"/api/v1/voice/conversations/{conversation_id}",
            headers=headers)
        assert state.status_code == 200
        assert state.json()["pending_confirmation"] is True
        done = client.post(
            f"/api/v1/voice/conversations/{conversation_id}/say",
            headers=headers, json={"text": "yes"})
        assert done.status_code == 200
        assert done.json()["task"]["kind"] == "task"
        stopped = client.post(
            f"/api/v1/voice/conversations/{conversation_id}/interrupt",
            headers=headers, json={})
        assert stopped.status_code == 200
        assert stopped.json()["interrupted"] is True


def test_auth_and_boundaries(tmp_path):
    _plane, client = _setup(tmp_path)
    with client:
        assert client.post("/api/v1/voice/conversations",
                           json={}).status_code == 401
        _session, _token, headers = login(client)
        assert client.post(
            "/api/v1/voice/conversations/missing/say", headers=headers,
            json={"text": "hi"}).status_code == 404
        started = client.post("/api/v1/voice/conversations",
                              headers=headers, json={}).json()
        conversation_id = started["conversation_id"]
        assert client.post(
            f"/api/v1/voice/conversations/{conversation_id}/say",
            headers=headers, json={"text": ""}).status_code == 400
        assert client.post(
            f"/api/v1/voice/conversations/{conversation_id}/say",
            headers=headers, json={"text": "a", "audio_b64": "x"}
        ).status_code == 400
        assert client.get(
            "/api/v1/voice/conversations/missing",
            headers=headers).status_code == 404


def test_cross_session_isolation(tmp_path):
    _plane, client = _setup(tmp_path)
    with client:
        _session, _token, alice = login(client)
        started = client.post("/api/v1/voice/conversations",
                              headers=alice, json={}).json()
        conversation_id = started["conversation_id"]
        _bs, _bt, bob = login(client, actor="bob")
        assert client.get(
            f"/api/v1/voice/conversations/{conversation_id}",
            headers=bob).status_code == 404
        assert client.post(
            f"/api/v1/voice/conversations/{conversation_id}/say",
            headers=bob, json={"text": "yes"}).status_code == 404
