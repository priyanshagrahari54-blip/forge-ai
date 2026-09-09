"""Orchestration API integration tests (A38)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo, \
    wait_for  # noqa: E402

from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)


def allow_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="agents-ok", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="writes-ok", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])


def _setup(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=allow_policy(),
                       approval_timeout=15.0)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def test_auth_and_validation_boundaries(tmp_path):
    _plane, client = _setup(tmp_path)
    with client:
        assert client.post("/api/v1/orchestrations", json={
            "requirement": "do things"}).status_code == 401
        assert client.get("/api/v1/orchestrations").status_code == 401
        _session, _token, headers = login(client)
        assert client.post("/api/v1/orchestrations", headers=headers,
                           json={"requirement": ""}).status_code == 400
        assert client.post("/api/v1/orchestrations", headers=headers,
                           json={"requirement": "x" * 8001}
                           ).status_code == 400
        assert client.get("/api/v1/orchestrations/nope",
                          headers=headers).status_code == 404


def test_full_lifecycle_over_api(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "analyze the "
                                 "repository and review the structure",
                                 "chain": True}).json()
        oid = submitted["orchestration_id"]
        assert submitted["status"] in ("QUEUED", "RUNNING")

        def done():
            record = client.get(f"/api/v1/orchestrations/{oid}",
                                headers=headers).json()
            return record if record["status"] in (
                "SUCCEEDED", "FAILED", "CANCELLED") else None
        record = wait_for(done, timeout=60)
        assert record["status"] == "SUCCEEDED"
        assert record["report"]["accepted"] is True
        assert record["plan"]["steps"]
        assert record["report"]["steps"]
        listed = client.get("/api/v1/orchestrations",
                            headers=headers).json()["orchestrations"]
        assert any(item["orchestration_id"] == oid for item in listed)


def test_deny_decision_over_api(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "Add CSV export "
                                 "functionality to this project",
                                      "chain": True}).json()
        oid = submitted["orchestration_id"]

        def pending():
            return client.get(
                f"/api/v1/orchestrations/{oid}/approvals",
                headers=headers).json()["approvals"]
        approvals = wait_for(lambda: pending() or None, timeout=20)
        assert approvals
        denied = client.post(
            f"/api/v1/orchestrations/{oid}/approvals/"
            f"{approvals[0]['id']}/deny", headers=headers, json={}).json()
        assert denied["approval"]["status"] == "denied"

        def done():
            record = plane.orchestrations.get(oid)
            return record if record.status.value in (
                "SUCCEEDED", "FAILED", "CANCELLED") else None
        record = wait_for(done, timeout=30)
        assert record.status.value == "FAILED"
        assert "export_csv" not in (
            Path(plane.projects["demo"].root) / "app.py").read_text()


def test_approval_decision_conflicts(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "Add CSV export "
                                 "functionality to this project",
                                      "chain": True}).json()
        oid = submitted["orchestration_id"]

        def pending():
            return client.get(
                f"/api/v1/orchestrations/{oid}/approvals",
                headers=headers).json()["approvals"]
        approvals = wait_for(lambda: pending() or None, timeout=20)
        approval_id = approvals[0]["id"]
        first = client.post(
            f"/api/v1/orchestrations/{oid}/approvals/{approval_id}/approve",
            headers=headers, json={})
        assert first.status_code == 200
        replay = client.post(
            f"/api/v1/orchestrations/{oid}/approvals/{approval_id}/approve",
            headers=headers, json={})
        assert replay.status_code == 409
        # A stranger's approval id maps to NOT_FOUND.
        assert client.post(
            f"/api/v1/orchestrations/{oid}/approvals/missing/approve",
            headers=headers, json={}).status_code == 404


def test_cancel_endpoint(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "analyze the "
                                      "repository", "chain": True}).json()
        oid = submitted["orchestration_id"]
        response = client.post(f"/api/v1/orchestrations/{oid}/cancel",
                               headers=headers, json={})
        assert response.status_code in (200, 409)

        def done():
            record = plane.orchestrations.get(oid)
            return record if record.status.value in (
                "SUCCEEDED", "FAILED", "CANCELLED") else None
        record = wait_for(done, timeout=60)
        assert record.status.value in ("SUCCEEDED", "CANCELLED")
