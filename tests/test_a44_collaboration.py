"""AI-to-AI collaboration (A44): untrusted external contributions,
permission gating, honesty labels."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.collaboration.connectors import (  # noqa: E402
    SimulatedExternalAIConnector,
    build_connector,
)
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def model_policy(effect: str = "ALLOW") -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="m-call", resource=Resource.MODEL,
                       operation="call", scope="", effect=effect,
                       provider="simulated-external"),
    ])


def test_connector_is_honest_and_bounded():
    connector = SimulatedExternalAIConnector()
    assert connector.available() is True
    response = connector.ask("how do I improve this function?")
    assert response["source"] == "external_ai"
    assert response["untrusted"] is True
    assert response["simulation"] is True
    assert "untrusted" in response["content"].lower()
    with pytest.raises(ValueError):
        connector.ask("")
    with pytest.raises(ValueError):
        build_connector("chatgpt-live")  # unknown → fail closed


def test_consult_is_policy_gated_and_marked(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=model_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        result = plane.collaboration_consult(
            session, "suggest a design for the exporter")
        assert result["allowed"] is True
        assert result["response"]["untrusted"] is True
        assert result["response"]["simulation"] is True
        assert result["response"]["source"] == "external_ai"
        history = plane.collaboration_history(session)
        assert len(history["history"]) == 1
        # The response never reaches the workspace or task list.
        runs = plane.runs.list_for_project(session.project_id)[0]
        assert not runs or all(
            run.requirement != "suggest a design for the exporter"
            for run in runs)


def test_consult_denied_fails_closed(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=model_policy("DENY"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        result = plane.collaboration_consult(session, "anything")
        assert result["allowed"] is False
        assert result["response"] is None
        assert "deny" in result["reason"].lower()


def test_consult_approval_round_trip(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=model_policy("REQUIRE_APPROVAL"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        first = plane.collaboration_consult(session, "is this ok?")
        assert first["allowed"] is False
        approval_id = first["approval_request_id"]
        assert any(item["id"] == approval_id
                   for item in plane.list_collaboration_approvals(session))
        decided = plane.decide_collaboration_approval(
            session, approval_id, True)
        second = plane.collaboration_consult(
            session, "is this ok?", approval_id=decided["token_id"])
        assert second["allowed"] is True
        assert second["response"]["untrusted"] is True
        # Token replay fails closed into a fresh approval.
        replay = plane.collaboration_consult(
            session, "is this ok?", approval_id=decided["token_id"])
        assert replay["allowed"] is False
        assert replay["approval_request_id"] != approval_id


def test_cross_session_isolation(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=model_policy("REQUIRE_APPROVAL"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        alice = plane.sessions.get(payload["session_id"])
        first = plane.collaboration_consult(alice, "hello?")
        approval_id = first["approval_request_id"]
        _bs, _bt, _bh = login(client, actor="bob")
        bob = plane.sessions.get(_bs["session_id"])
        assert plane.list_collaboration_approvals(bob) == []
        with pytest.raises(Exception):
            plane.decide_collaboration_approval(bob, approval_id, True)
        # Bob's own history is separate.
        assert plane.collaboration_history(bob)["history"] == []


def test_collaboration_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=model_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/ai-to-ai/capabilities").status_code == 401
        _session, _token, headers = login(client)
        caps = client.get("/api/v1/ai-to-ai/capabilities",
                          headers=headers).json()
        assert caps["untrusted_by_design"] is True
        assert caps["simulated_only"] is True
        consult = client.post("/api/v1/ai-to-ai/consult", headers=headers,
                              json={"question": "any advice?"})
        assert consult.status_code == 200
        assert consult.json()["response"]["untrusted"] is True
        assert client.post("/api/v1/ai-to-ai/consult", headers=headers,
                           json={"question": ""}).status_code == 400
        assert client.post("/api/v1/ai-to-ai/consult", headers=headers,
                           json={"question": "x",
                                 "provider": "chatgpt-live"}
                           ).status_code == 400
        history = client.get("/api/v1/ai-to-ai/history", headers=headers)
        assert history.status_code == 200
        assert history.json()["history"]
