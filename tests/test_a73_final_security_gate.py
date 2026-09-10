"""Final security gate (A73): go/no-go on live security evidence."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def constrained_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
        PermissionRule(id="deny-net", resource=Resource.NETWORK,
                       operation="request", scope="example.com",
                       effect="DENY"),
        PermissionRule(id="term", resource=Resource.TERMINAL,
                       operation="execute", scope="src",
                       effect="REQUIRE_APPROVAL",
                       args=("/usr/bin/python", "-m", "pytest")),
    ])


def test_security_gate_passes_on_a_clean_plane(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=constrained_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_security_gate(session)
        assert report["passed"] is True
        checks = {entry["check"]: entry for entry in report["checks"]}
        assert checks["policy_rule_integrity"]["passed"] is True
        assert checks["no_secret_hits"]["passed"] is True
        assert checks["posture"]["passed"] is True
        assert checks["sessions_bounded"]["passed"] is True
        assert "changes nothing" in report["note"]


def test_security_gate_flags_planted_secrets(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=constrained_policy())
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    (root / "leak.txt").write_text(
        "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD\n",
        encoding="utf-8")
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_security_gate(session)
        assert report["passed"] is False
        checks = {entry["check"]: entry for entry in report["checks"]}
        assert checks["no_secret_hits"]["passed"] is False
        assert "1 secret-pattern hit" in \
            checks["no_secret_hits"]["evidence"]


def test_all_allow_policy_fails_the_posture_check(tmp_path):
    wide = PermissionPolicy(rules=[
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
        PermissionRule(id="term", resource=Resource.TERMINAL,
                       operation="execute", scope="src",
                       effect="ALLOW",
                       args=("/usr/bin/python", "-m", "pytest")),
    ])
    plane = make_plane(tmp_path, start=True, policy=wide)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_security_gate(session)
        assert report["passed"] is False
        checks = {entry["check"]: entry for entry in report["checks"]}
        assert checks["posture"]["passed"] is False
        assert "nothing constrains" in checks["posture"]["evidence"]


def test_fail_closed_policy_passes_as_safe(tmp_path):
    plane = make_plane(tmp_path, start=True)  # empty policy
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_security_gate(session)
        assert report["passed"] is True
        checks = {entry["check"]: entry for entry in report["checks"]}
        assert "fail closed" in checks["posture"]["evidence"]


def test_security_gate_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=constrained_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        response = client.post("/api/v1/final/security", headers=headers)
        assert response.status_code == 200
        assert response.json()["passed"] is True
