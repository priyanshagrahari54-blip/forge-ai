"""Computer-use API integration tests (A40)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402
from helpers_a39 import b64, make_png  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def computer_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="v-analyze", resource=Resource.VISION,
                       operation="analyze", scope="", effect="ALLOW"),
        PermissionRule(id="v-execute", resource=Resource.VISION,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="d-move", resource=Resource.DESKTOP,
                       operation="mouse_move", scope="", effect="ALLOW"),
        PermissionRule(id="d-proc", resource=Resource.DESKTOP,
                       operation="process", scope="", effect="ALLOW"),
        PermissionRule(id="d-shot", resource=Resource.DESKTOP,
                       operation="screenshot", scope="", effect="ALLOW"),
    ])


def _setup(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=computer_policy())
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def _grant(client, headers, task_id, scopes):
    response = client.post("/api/v1/desktop/grants", headers=headers,
                           json={"task_id": task_id, "scopes": scopes})
    assert response.status_code == 200, response.text
    return response.json()


def test_auth_boundaries(tmp_path):
    _plane, client = _setup(tmp_path)
    with client:
        assert client.get("/api/v1/computer/history").status_code == 401
        assert client.get("/api/v1/computer/approvals").status_code == 401
        assert client.post("/api/v1/computer/observe", json={
            "image_b64": "AAAA"}).status_code == 401
        assert client.post("/api/v1/computer/act", json={
            "action": "mouse_move"}).status_code == 401
        _session, _token, headers = login(client)
        assert client.post("/api/v1/computer/observe", headers=headers,
                           json={"image_b64": ""}).status_code == 400
        assert client.post("/api/v1/computer/observe", headers=headers,
                           json={"image_b64": "!!!"}).status_code == 400
        assert client.post("/api/v1/computer/act", headers=headers,
                           json={"action": "teleport"}
                           ).status_code == 400
        assert client.post("/api/v1/computer/act", headers=headers,
                           json={"action": "mouse_move",
                                 "target": "x" * 600}
                           ).status_code == 400


def test_observe_propose_cycle_endpoints(tmp_path):
    _plane, client = _setup(tmp_path)
    with client:
        session, _token, headers = login(client)
        observed = client.post("/api/v1/computer/observe", headers=headers,
                               json={"image_b64": b64(make_png()),
                                     "goal": "click OK"})
        assert observed.status_code == 200
        payload = observed.json()
        assert payload["snapshot_version"] == 1
        assert payload["element_tree"]["kind"] == "screen"
        proposed = client.post("/api/v1/computer/propose", headers=headers,
                               json={"image_b64": b64(make_png()),
                                     "goal": "click OK"})
        assert proposed.status_code == 200
        assert proposed.json()["executed"] is False
        _grant(client, headers, session["session_id"], ["input"])
        cycled = client.post("/api/v1/computer/cycle", headers=headers,
                             json={"image_b64": b64(make_png()),
                                   "goal": "click OK"})
        assert cycled.status_code == 200
        assert "cycle" in cycled.json()["note"].lower()
        history = client.get("/api/v1/computer/history", headers=headers)
        assert history.status_code == 200
        assert history.json()["snapshots"]


def test_act_endpoint_executes_and_redacts(tmp_path):
    _plane, client = _setup(tmp_path)
    with client:
        session, _token, headers = login(client, profile="autonomous")
        _grant(client, headers, session["session_id"], ["input"])
        acted = client.post("/api/v1/computer/act", headers=headers,
                            json={"action": "mouse_move",
                                  "params": {"x": 4, "y": 5},
                                  "reason": "move"})
        assert acted.status_code == 200
        assert acted.json()["executed"] is True
        typed = client.post("/api/v1/computer/act", headers=headers,
                            json={"action": "mouse_move",
                                  "params": {"x": 1, "y": 1, "secret": "s3k"},
                                  "reason": "move with a stray param"})
        assert typed.status_code == 200
        history = client.get("/api/v1/computer/history",
                             headers=headers).json()
        assert "<redacted" in str(history["actions"])


def test_high_risk_escalation_and_approval_round_trip(tmp_path):
    _plane, client = _setup(tmp_path)
    with client:
        session, _token, headers = login(client, profile="autonomous")
        _grant(client, headers, session["session_id"], ["process:*"])
        escalated = client.post("/api/v1/computer/act", headers=headers,
                                json={"action": "process",
                                      "target": "browser",
                                      "params": {"op": "terminate"},
                                      "reason": "stop browser"})
        assert escalated.status_code == 200
        payload = escalated.json()
        assert payload["approval_required"] is True
        approval_id = payload["approval_request_id"]
        listed = client.get("/api/v1/computer/approvals",
                            headers=headers).json()["approvals"]
        assert any(item["id"] == approval_id for item in listed)
        decided = client.post(
            f"/api/v1/computer/approvals/{approval_id}/approve",
            headers=headers, json={})
        assert decided.status_code == 200
        assert decided.json()["token_id"]


def test_decision_conflicts_and_isolation(tmp_path):
    _plane, client = _setup(tmp_path)
    with client:
        session, _token, headers = login(client, profile="autonomous")
        _grant(client, headers, session["session_id"], ["process:*"])
        escalated = client.post("/api/v1/computer/act", headers=headers,
                                json={"action": "process",
                                      "target": "browser",
                                      "params": {"op": "terminate"},
                                      "reason": "stop browser"}).json()
        approval_id = escalated["approval_request_id"]
        ok = client.post(
            f"/api/v1/computer/approvals/{approval_id}/deny",
            headers=headers, json={})
        assert ok.status_code == 200
        replay = client.post(
            f"/api/v1/computer/approvals/{approval_id}/deny",
            headers=headers, json={})
        assert replay.status_code == 409
        assert client.post("/api/v1/computer/approvals/nope/deny",
                           headers=headers, json={}).status_code == 404
        _bs, _bt, bob_headers = login(client, actor="bob")
        assert client.get("/api/v1/computer/approvals",
                          headers=bob_headers).json()["approvals"] == []
