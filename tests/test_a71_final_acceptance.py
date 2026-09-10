"""Final acceptance (A71): bounded end-to-end checklist."""
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


def working_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
        PermissionRule(id="term", resource=Resource.TERMINAL,
                       operation="execute", scope="src",
                       effect="ALLOW",
                       args=("/usr/bin/python", "-m", "pytest")),
    ])


def test_acceptance_passes_on_a_working_plane(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=working_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_acceptance(session)
        assert report["total"] == 6
        assert report["accepted"] is True
        names = {entry["check"] for entry in report["checks"]}
        assert names == {"projects_registered", "fabric_usable",
                         "policy_present", "database_live",
                         "agent_gate_surface", "smoke_run"}
        smoke = next(entry for entry in report["checks"]
                     if entry["check"] == "smoke_run")
        assert smoke["passed"] is True
        # The smoke really ran through the pipeline.
        assert smoke["evidence"].startswith("smoke run SUCCEEDED")


def test_acceptance_fails_honestly_without_a_policy(tmp_path):
    plane = make_plane(tmp_path, start=True)  # default empty policy
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_acceptance(session)
        assert report["accepted"] is False
        policy_entry = next(entry for entry in report["checks"]
                            if entry["check"] == "policy_present")
        assert policy_entry["passed"] is False
        smoke = next(entry for entry in report["checks"]
                     if entry["check"] == "smoke_run")
        # Without a policy every action went through the approval
        # gate, which the harness operated honestly — but the missing
        # policy still fails acceptance overall.
        assert smoke["passed"] is True
        assert smoke["evidence"].startswith("smoke run SUCCEEDED")


def test_acceptance_reports_a_real_smoke_failure(tmp_path):
    # The pipeline itself fails (denied writes), and acceptance says
    # so instead of papering over it.
    plane = make_plane(tmp_path, start=True, policy=working_policy())
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    (root / "app.py").chmod(0)  # break the project so coding fails
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_acceptance(session)
        smoke = next(entry for entry in report["checks"]
                     if entry["check"] == "smoke_run")
        assert smoke["passed"] is False
        assert "FAILED" in smoke["evidence"]


def test_acceptance_checklist_structure_is_stable(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=working_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_acceptance(session)
        for entry in report["checks"]:
            assert set(entry) >= {"check", "passed", "evidence"}
            assert isinstance(entry["passed"], bool)
        assert "no check is pre-marked passed" in report["note"]


def test_acceptance_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=working_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        response = client.post("/api/v1/final/acceptance", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is True
        assert body["passed"] == body["total"]
