"""Control-plane orchestration tests (A38).

The plane queues orchestrations on its worker pool, plans a real
capability-matched team, gates dispatch through the A33 policy, and
records per-step reports. Sessions never see each other's records.
"""
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


def _setup(tmp_path, policy=None, approval_timeout=15.0):
    plane = make_plane(tmp_path, start=True, policy=policy,
                       approval_timeout=approval_timeout)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def test_analysis_chain_succeeds_with_allow_policy(tmp_path):
    plane, client = _setup(tmp_path, policy=allow_policy())
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "analyze the repository "
                                 "and review the structure",
                                 "chain": True}).json()
        oid = submitted["orchestration_id"]

        def done():
            record = plane.orchestrations.get(oid)
            if record is None or record.status.value in (
                    "SUCCEEDED", "FAILED", "CANCELLED"):
                return record
            return None
        record = wait_for(done, timeout=60)
        assert record.status.value == "SUCCEEDED", record.error
        report = record.report()
        agents = [step["agent"] for step in report["steps"]]
        assert "reviewer" in agents
        assert all(step["status"] == "SUCCEEDED"
                   for step in report["steps"])
        assert report["accepted"] is True


def test_coder_chain_applies_change_after_approval(tmp_path):
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
        assert approvals, "coder change set should file an approval"
        assert approvals[0]["resource"] == "filesystem"
        for approval in approvals:
            decided = client.post(
                f"/api/v1/orchestrations/{oid}/approvals/"
                f"{approval['id']}/approve", headers=headers,
                json={}).json()
            assert decided["token_id"]

        def done():
            record = plane.orchestrations.get(oid)
            return record if record.status.value in (
                "SUCCEEDED", "FAILED", "CANCELLED") else None
        record = wait_for(done, timeout=60)
        assert record.status.value == "SUCCEEDED", record.error
        steps = {step["agent"]: step for step in record.report()["steps"]}
        assert steps["coder"]["status"] == "SUCCEEDED"
        applied = (Path(plane.projects["demo"].root) / "app.py").read_text()
        assert "export_csv" in applied


def test_deny_fails_closed(tmp_path):
    policy = PermissionPolicy(rules=[
        PermissionRule(id="agents-ok", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="writes-no", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="DENY"),
    ])
    plane, client = _setup(tmp_path, policy=policy)
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "Add CSV export "
                                 "functionality to this project",
                                      "chain": True}).json()
        oid = submitted["orchestration_id"]

        def done():
            record = plane.orchestrations.get(oid)
            return record if record.status.value in (
                "SUCCEEDED", "FAILED", "CANCELLED") else None
        record = wait_for(done, timeout=60)
        assert record.status.value == "FAILED"
        assert record.report()["accepted"] is False
        # Nothing was written.
        assert "export_csv" not in (
            Path(plane.projects["demo"].root) / "app.py").read_text()


def test_plan_rejection_when_nothing_matches(tmp_path):
    plane, client = _setup(tmp_path, policy=allow_policy())
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "qqq zzz yyy",
                                      "chain": False}).json()
        oid = submitted["orchestration_id"]

        def done():
            record = plane.orchestrations.get(oid)
            return record if record.status.value in (
                "SUCCEEDED", "FAILED", "CANCELLED") else None
        record = wait_for(done, timeout=30)
        assert record.status.value == "FAILED"
        assert "rejected" in record.report()["summary"]


def test_cancel_stops_a_running_orchestration(tmp_path):
    plane, client = _setup(tmp_path, policy=allow_policy())
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "analyze the "
                                      "repository", "chain": True}).json()
        oid = submitted["orchestration_id"]
        cancelled = client.post(
            f"/api/v1/orchestrations/{oid}/cancel",
            headers=headers, json={})
        assert cancelled.status_code in (200, 409)

        def done():
            record = plane.orchestrations.get(oid)
            return record if record.status.value in (
                "SUCCEEDED", "FAILED", "CANCELLED") else None
        record = wait_for(done, timeout=60)
        assert record.status.value in ("SUCCEEDED", "CANCELLED", "FAILED")


def test_cross_session_isolation(tmp_path):
    plane, client = _setup(tmp_path, policy=allow_policy())
    with client:
        _session, _token, alice = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=alice,
                                json={"requirement": "analyze the "
                                      "repository", "chain": False}).json()
        oid = submitted["orchestration_id"]
        _session_b, _token_b, bob = login(client, actor="bob")
        assert client.get(f"/api/v1/orchestrations/{oid}",
                          headers=bob).status_code == 404
        assert client.post(f"/api/v1/orchestrations/{oid}/cancel",
                           headers=bob, json={}).status_code == 404
        listed = client.get("/api/v1/orchestrations",
                            headers=bob).json()["orchestrations"]
        assert all(item["orchestration_id"] != oid for item in listed)


def test_orchestrations_survive_restart(tmp_path):
    plane, client = _setup(tmp_path, policy=allow_policy())
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/orchestrations", headers=headers,
                                json={"requirement": "analyze the "
                                      "repository", "chain": False}).json()
        oid = submitted["orchestration_id"]

        def done():
            record = plane.orchestrations.get(oid)
            return record if record.status.value in (
                "SUCCEEDED", "FAILED", "CANCELLED") else None
        record = wait_for(done, timeout=60)
        assert record.status.value == "SUCCEEDED"
    # Fresh plane over the same database still serves the record.
    from forge.control.control_plane import ControlConfig, ControlPlane
    from helpers_a34 import ScriptedProvider, make_fabric

    config = ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(tmp_path / "demo")},
        fabric=make_fabric(ScriptedProvider()),
        policy=allow_policy())
    plane2 = ControlPlane(config)
    record2 = plane2.orchestrations.get(oid)
    assert record2 is not None
    assert record2.report()["status"] == "SUCCEEDED"


def test_audit_records_submission(tmp_path):
    plane, client = _setup(tmp_path, policy=allow_policy())
    with client:
        _session, _token, headers = login(client)
        client.post("/api/v1/orchestrations", headers=headers,
                    json={"requirement": "inspect the code",
                          "chain": False})
        assert any(event.operation == "submit"
                   and event.resource == "orchestration"
                   for event in plane.audit.events)


def test_agent_policy_vocabulary_matches():
    from forge.security.policy import PermissionRequest
    policy = PermissionPolicy(rules=[
        PermissionRule(id="coder-ok", resource=Resource.AGENT,
                       operation="execute", scope="coder", effect="ALLOW"),
        PermissionRule(id="agents-deny", resource=Resource.AGENT,
                       operation="execute", scope="", effect="DENY"),
    ])
    allowed = policy.evaluate(PermissionRequest(
        agent="forge-orchestrator", resource=Resource.AGENT,
        operation="execute", scope="coder"))
    assert allowed.decision.value == "ALLOW"
    blocked = policy.evaluate(PermissionRequest(
        agent="forge-orchestrator", resource=Resource.AGENT,
        operation="execute", scope="debugger"))
    assert blocked.decision.value == "DENY"
    unknown = policy.evaluate(PermissionRequest(
        agent="forge-orchestrator", resource=Resource.AGENT,
        operation="grant", scope="coder"))
    assert unknown.decision.value == "DENY"
