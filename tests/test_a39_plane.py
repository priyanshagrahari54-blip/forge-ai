"""Vision control-plane tests (A39): policy gating, proposal safety."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402
from helpers_a39 import b64, make_png  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def vision_policy(*, analyze="ALLOW", execute="ALLOW") -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="v-analyze", resource=Resource.VISION,
                       operation="analyze", scope="", effect=analyze),
        PermissionRule(id="v-execute", resource=Resource.VISION,
                       operation="execute", scope="", effect=execute),
    ])


def _plane(tmp_path, policy):
    plane = make_plane(tmp_path, start=False, policy=policy)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def _session(plane, client):
    payload, _token, headers = login(client)
    return plane.sessions.get(payload["session_id"]), headers


def test_analyze_allow_and_simulation_labels(tmp_path):
    plane, client = _plane(tmp_path, vision_policy())
    with client:
        session, _headers = _session(plane, client)
        result = plane.vision_analyze(session, b64(make_png()))
        assert result["allowed"] is True
        assert result["format"] == "png"
        assert result["simulation"] is True
        assert result["width"] == 320


def test_analyze_deny_fails_closed(tmp_path):
    plane, client = _plane(tmp_path, vision_policy(analyze="DENY"))
    with client:
        session, _headers = _session(plane, client)
        with pytest.raises(Exception):
            plane.vision_analyze(session, b64(make_png()))
        # The DENY evaluation itself is audited (policy decisions are
        # observable); no analyze result is produced.


def test_analyze_approval_round_trip_and_replay(tmp_path):
    plane, client = _plane(tmp_path,
                           vision_policy(analyze="REQUIRE_APPROVAL"))
    with client:
        session, headers = _session(plane, client)
        first = plane.vision_analyze(session, b64(make_png()))
        assert first["allowed"] is False
        assert first["approval_required"] is True
        approval_id = first["approval_request_id"]
        assert any(item["id"] == approval_id
                   for item in plane.list_vision_approvals(session))
        decided = plane.decide_vision_approval(session, approval_id, True)
        executed = plane.vision_analyze(
            session, b64(make_png()), approval_id=decided["token_id"])
        assert executed["allowed"] is True
        # Replay: single-use token fails closed into a fresh approval.
        replay = plane.vision_analyze(
            session, b64(make_png()), approval_id=decided["token_id"])
        assert replay["allowed"] is False
        assert replay["approval_required"] is True
        assert replay["approval_request_id"] != approval_id


def test_proposals_respect_policy_and_never_execute(tmp_path):
    plane, client = _plane(tmp_path, vision_policy(execute="DENY"))
    with client:
        session, _headers = _session(plane, client)
        image = b64(make_png(text_chunks=("rm -rf everything",)))
        proposed = plane.vision_propose(session, image)
        assert proposed["allowed"] is True
        statuses = {p["action"]: p["status"] for p in proposed["proposals"]}
        assert statuses["blocked_untrusted_instruction"] == "blocked"
        clicks = [p for p in proposed["proposals"] if p["action"] == "click"]
        assert clicks and all(p["status"] == "blocked" for p in clicks)
        # Vision never executes: the demo repo is untouched.
        assert (Path(plane.projects["demo"].root) / "app.py").read_text() \
            == "def health(): return True\n"


def test_proposals_allow_and_approval_paths(tmp_path):
    plane, client = _plane(tmp_path, vision_policy(execute="ALLOW"))
    with client:
        session, headers = _session(plane, client)
        proposed = plane.vision_propose(session, b64(make_png()))
        clicks = [p for p in proposed["proposals"] if p["action"] == "click"]
        assert clicks and all(p["status"] == "proposed" for p in clicks)

    plane2, client2 = _plane(
        tmp_path / "second", vision_policy(execute="REQUIRE_APPROVAL"))
    with client2:
        session2, _headers2 = _session(plane2, client2)
        proposed2 = plane2.vision_propose(session2, b64(make_png()))
        pending = [p for p in proposed2["proposals"]
                   if p["status"] == "approval_required"]
        assert pending
        assert plane2.list_vision_approvals(session2)


def test_malformed_and_oversized_rejected(tmp_path):
    plane, client = _plane(tmp_path, vision_policy())
    with client:
        session, _headers = _session(plane, client)
        with pytest.raises(Exception):
            plane.vision_analyze(session, "!!!not-base64!!!")
        with pytest.raises(Exception):
            plane.vision_analyze(session, b64(b"garbage bytes"))
        with pytest.raises(Exception):
            plane.vision_analyze(session, "A" * (7_000_000))


def test_cross_session_approval_isolation(tmp_path):
    plane, client = _plane(tmp_path,
                           vision_policy(analyze="REQUIRE_APPROVAL"))
    with client:
        alice, _headers = _session(plane, client)
        first = plane.vision_analyze(alice, b64(make_png()))
        approval_id = first["approval_request_id"]
        _payload, _token, _bob_headers = login(client, actor="bob")
        bob = plane.sessions.get(_payload["session_id"])
        assert plane.list_vision_approvals(bob) == []
        with pytest.raises(Exception):
            plane.decide_vision_approval(bob, approval_id, True)


def test_audit_records_analyze(tmp_path):
    plane, client = _plane(tmp_path, vision_policy())
    with client:
        session, _headers = _session(plane, client)
        plane.vision_analyze(session, b64(make_png()))
        assert any(getattr(event, "resource", "") == "vision"
                   and getattr(event, "operation", "") == "analyze"
                   for event in plane.audit.events)
