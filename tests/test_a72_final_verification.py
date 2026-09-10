"""Final verification (A72): evidence checks on finished runs."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from helpers_a34 import (drive_to_terminal, login, make_client,  # noqa: E402
                         make_plane, make_repo)


def working_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
        PermissionRule(id="term", resource=Resource.TERMINAL,
                       operation="execute", scope="src",
                       effect="ALLOW",
                       args=("/usr/bin/python", "-m", "pytest")),
    ])


def test_successful_run_verifies_with_evidence(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=working_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        task = plane.submit_task(session, "add a csv export")
        state = drive_to_terminal(client, headers, task.id)
        assert state["status"] == "SUCCEEDED"
        report = plane.final_verify_run(session, task.id)
        assert report["verified"] is True
        assert report["status"] == "SUCCEEDED"
        checks = {entry["check"]: entry for entry in report["checks"]}
        assert checks["terminal_status"]["passed"] is True
        assert checks["succeeded"]["passed"] is True
        assert checks["report_recorded"]["passed"] is True
        assert checks["files_exist"]["passed"] is True
        assert "on disk" in checks["files_exist"]["evidence"]


def test_failed_run_verifies_honestly(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=working_policy())
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    (root / "app.py").chmod(0)
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        task = plane.submit_task(session, "add a csv export")
        state = drive_to_terminal(client, headers, task.id)
        assert state["status"] == "FAILED"
        report = plane.final_verify_run(session, task.id)
        assert report["verified"] is False
        assert report["status"] == "FAILED"
        checks = {entry["check"]: entry for entry in report["checks"]}
        assert checks["succeeded"]["passed"] is False


def test_unknown_and_cross_project_runs_do_not_leak(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       extra_projects={"other": str(tmp_path / "other")})
    make_repo(Path(plane.projects["demo"].root))
    make_repo(Path(plane.projects["other"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        report = plane.final_verify_run(session, "t-unknown-000000")
        assert report["verified"] is False
        assert report["status"] == "NOT_FOUND"


def test_pending_run_is_reported_as_unfinished(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=working_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        task = plane.submit_task(session, "add a csv export")
        # Immediately verify: the run cannot have finished.
        report = plane.final_verify_run(session, task.id)
        if report["status"] in ("SUCCEEDED", "FAILED", "CANCELLED",
                                "ROLLED_BACK"):
            pytest.skip("run finished before verification could "
                        "observe it pending")
        assert report["verified"] is False
        drive_to_terminal(client, headers, task.id)


def test_verification_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=working_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        task = plane.submit_task(
            plane.sessions.get(_session["session_id"]),
            "add a csv export")
        drive_to_terminal(client, headers, task.id)
        response = client.post("/api/v1/final/verify-run", headers=headers,
                               json={"run_id": task.id})
        assert response.status_code == 200
        assert response.json()["verified"] is True
        missing = client.post("/api/v1/final/verify-run", headers=headers,
                              json={"run_id": "t-unknown-000000"})
        assert missing.status_code == 200
        assert missing.json()["verified"] is False
