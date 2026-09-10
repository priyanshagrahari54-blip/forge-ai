"""Cockpit security tests (A34): the browser is never trusted."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    login,
    make_client,
    make_plane,
    make_repo,
    task_state,
    wait_for,
)


def _setup(tmp_path, **kwargs):
    plane = make_plane(tmp_path, **kwargs)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane)


def test_malformed_ids_fail_closed(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        for bad in ("../x", "..%2f..%2fetc", "/abs/path", "a/b",
                    "t-foo;rm", "", "x" * 200):
            response = client.get(f"/api/v1/tasks/{bad}", headers=headers)
            assert response.status_code in (400, 404), bad
            if response.status_code == 400:
                assert response.json()["error"]["code"] == "INVALID_REQUEST"
            assert "Traceback" not in response.text


def test_cross_project_task_access_is_404(tmp_path):
    other = tmp_path / "other"
    plane = make_plane(tmp_path, extra_projects={"other": str(other)})
    make_repo(Path(plane.projects["demo"].root))
    (other / "note.txt").write_text("other project")
    client = make_client(plane)
    with client:
        _, _, demo_headers = login(client, project_id="demo")
        _, _, other_headers = login(client, actor="bob",
                                    project_id="other")
        created = client.post("/api/v1/tasks", headers=other_headers,
                              json={"requirement": "Other secret task"})
        task_id = created.json()["task"]["task_id"]
        # Same id from the wrong project: indistinguishable from missing.
        response = client.get(f"/api/v1/tasks/{task_id}",
                              headers=demo_headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"
        events = client.get(f"/api/v1/tasks/{task_id}/events",
                            headers=demo_headers)
        assert events.status_code == 404
        cancel = client.post(f"/api/v1/tasks/{task_id}/cancel",
                             headers=demo_headers, json={})
        assert cancel.status_code == 404
        # And the owner can still see it.
        assert task_state(client, other_headers, task_id)["task_id"] == \
            task_id


def test_cross_project_routes_rejected(tmp_path):
    other = tmp_path / "other"
    plane = make_plane(tmp_path, extra_projects={"other": str(other)})
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _, _, headers = login(client, project_id="demo")
        state = client.get("/api/v1/projects/other", headers=headers)
        assert state.status_code == 404
        git = client.get("/api/v1/projects/other/git", headers=headers)
        assert git.status_code == 404


def test_cross_project_approval_is_404(tmp_path):
    other = tmp_path / "other"
    plane = make_plane(tmp_path, extra_projects={"other": str(other)})
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _, _, demo_headers = login(client, project_id="demo")
        _, _, other_headers = login(client, actor="bob",
                                    project_id="other")
        created = client.post("/api/v1/tasks", headers=other_headers,
                              json={"requirement": "Other task"})
        assert created.json()["task"]["task_id"]

        def pending():
            approvals = client.get("/api/v1/approvals",
                                   headers=other_headers).json()["approvals"]
            return approvals[0]["id"] if approvals else None

        approval_id = wait_for(pending, timeout=60.0)
        # Invisible (and undecidable) from the other project.
        assert client.get("/api/v1/approvals", headers=demo_headers).json()[
            "approvals"] == []
        missing = client.get(f"/api/v1/approvals/{approval_id}",
                             headers=demo_headers)
        assert missing.status_code == 404
        decide = client.post(f"/api/v1/approvals/{approval_id}/approve",
                             headers=demo_headers)
        assert decide.status_code == 404
        # Owner decides normally.
        ok = client.post(f"/api/v1/approvals/{approval_id}/approve",
                         headers=other_headers)
        assert ok.status_code == 200


def test_approval_replay_and_double_decision(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        assert created.json()["task"]["task_id"]

        def pending():
            approvals = client.get("/api/v1/approvals",
                                   headers=headers).json()["approvals"]
            return approvals[0]["id"] if approvals else None

        approval_id = wait_for(pending, timeout=60.0)
        first = client.post(f"/api/v1/approvals/{approval_id}/approve",
                            headers=headers)
        assert first.status_code == 200
        assert first.json()["approval"]["duplicate"] is False
        # Idempotent retry by the same approver.
        second = client.post(f"/api/v1/approvals/{approval_id}/approve",
                             headers=headers)
        assert second.status_code == 200
        assert second.json()["approval"]["duplicate"] is True
        # A conflicting decision is rejected.
        conflict = client.post(f"/api/v1/approvals/{approval_id}/deny",
                               headers=headers)
        assert conflict.status_code == 409
        # Another approver cannot re-decide either.
        _, _, bob = login(client, actor="bob")
        other = client.post(f"/api/v1/approvals/{approval_id}/deny",
                            headers=bob)
        assert other.status_code == 409


def test_agent_cannot_self_authorize(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        assert created.json()["task"]["task_id"]

        def pending():
            approvals = client.get("/api/v1/approvals",
                                   headers=headers).json()["approvals"]
            return approvals[0] if approvals else None

        approval = wait_for(pending, timeout=60.0)
        # Log in AS the requesting agent and attempt self-approval.
        _, _, agent_headers = login(client, actor=approval["agent"])
        response = client.post(
            f"/api/v1/approvals/{approval['id']}/approve",
            headers=agent_headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"


def test_expired_approval_cannot_be_used(tmp_path):
    plane = make_plane(tmp_path, approval_timeout=1.0)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality"})
        assert created.json()["task"]["task_id"]

        def pending():
            approvals = client.get("/api/v1/approvals",
                                   headers=headers).json()["approvals"]
            return approvals[0]["id"] if approvals else None

        approval_id = wait_for(pending, timeout=60.0)
        time.sleep(1.2)
        # The waiter already gave up; a late decision is rejected.
        late = client.post(f"/api/v1/approvals/{approval_id}/approve",
                           headers=headers)
        assert late.status_code in (409, 410)
        assert late.json()["error"]["code"] in (
            "APPROVAL_CONFLICT", "APPROVAL_EXPIRED")


def test_csrf_required_for_cookie_mutations(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        # No bearer header: rely on the cookie jar alone.
        login(client)
        naked = client.post("/api/v1/tasks",
                            json={"requirement": "x"})
        assert naked.status_code == 403
        assert naked.json()["error"]["code"] == "CSRF_REQUIRED"
        guarded = client.post("/api/v1/tasks",
                              headers={"X-Requested-With": "forge-cockpit"},
                              json={"requirement": "Cookie task"})
        assert guarded.status_code == 200
        # Reads need no CSRF header.
        listed = client.get("/api/v1/tasks")
        assert listed.status_code == 200


def test_cors_defaults_to_no_wildcard(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        response = client.options(
            "/api/v1/tasks",
            headers={"Origin": "https://evil.example",
                     "Access-Control-Request-Method": "POST"})
        assert response.headers.get("access-control-allow-origin") != \
            "https://evil.example"


def test_allow_all_origins_rejected():
    from forge.api.app import ApiConfig, create_app

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        plane = make_plane(Path(raw))
        try:
            create_app(plane, ApiConfig(allowed_origins=["*"]))
        except ValueError:
            pass
        else:
            raise AssertionError("allow * must be rejected")
        finally:
            plane.close()


def test_oversized_and_malformed_requests_rejected(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        huge = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "x" * 9000})
        assert huge.status_code == 400
        body = client.post(
            "/api/v1/tasks",
            content=b"x" * (1024 * 1024 + 1),
            headers={**headers, "Content-Type": "application/json"})
        assert body.status_code == 413
        malformed = client.post(
            "/api/v1/tasks", content=b"{nope",
            headers={**headers, "Content-Type": "application/json"})
        assert malformed.status_code in (400, 422)
        assert "Traceback" not in malformed.text


def test_no_bypass_fields_on_task_submit(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": "Add CSV export functionality",
            "approved": True, "bypass": True, "admin": True,
            "skip_policy": True, "mode": "assisted"})
        assert created.status_code == 200
        task_id = created.json()["task"]["task_id"]

        def waiting_or_more():
            current = task_state(client, headers, task_id)
            if current["status"] in ("WAITING_APPROVAL", "RUNNING",
                                     "PAUSED"):
                return current
            approvals = client.get("/api/v1/approvals",
                                   headers=headers).json()["approvals"]
            if approvals:
                return current
            return None

        # Client-controlled flags must not skip the approval gate: the run
        # either waits for approval or already shows a pending request.
        wait_for(waiting_or_more, timeout=60.0)


def test_shell_text_is_inert(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        evil = "x; rm -rf / # $(touch /tmp/forge-pwned)"
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": evil})
        assert created.status_code == 200
        task_id = created.json()["task"]["task_id"]
        assert task_state(client, headers, task_id)["requirement"] == evil
        assert not Path("/tmp/forge-pwned").exists()
        unknown = client.post("/api/v1/commands", headers=headers, json={
            "command": "rm -rf"})
        assert unknown.status_code == 400
        denied = client.post("/api/v1/interpret", headers=headers, json={
            "text": "rm -rf the repository"})
        assert denied.json()["interpretation"]["kind"] == "DENY"
        assert denied.json()["executed"] is False


def test_secret_redaction_in_events(tmp_path):
    plane, client = _setup(tmp_path)
    secret = 'api_key = "sk-live-0123456789abcdef"'
    with client:
        _, _, headers = login(client)
        created = client.post("/api/v1/tasks", headers=headers, json={
            "requirement": f"Rotate {secret} everywhere"})
        task_id = created.json()["task"]["task_id"]
        events = client.get(f"/api/v1/tasks/{task_id}/events",
                            headers=headers).json()["events"]
        assert events
        blob = str(events)
        assert "sk-live-0123456789abcdef" not in blob


def test_error_responses_never_leak_tracebacks(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        _, _, headers = login(client)
        bad_calls = [
            ("GET", "/api/v1/tasks/%%%", None),
            ("GET", "/api/v1/approvals/..", None),
            ("POST", "/api/v1/tasks", {"requirement": 123}),
            ("POST", "/api/v1/commands", {"command": "NOPE"}),
            ("GET", "/api/v1/projects/%2e%2e/x", None),
        ]
        for method, path, payload in bad_calls:
            if method == "GET":
                response = client.get(path, headers=headers)
            else:
                response = client.post(path, headers=headers,
                                       json=payload)
            assert response.status_code in (400, 404, 409, 422), path
            assert "Traceback" not in response.text, path
            assert "File \"" not in response.text, path


def test_session_expiry_enforced(tmp_path):
    from helpers_a34 import make_fabric, ScriptedProvider
    from forge.control import ControlPlane, ControlConfig

    root = tmp_path / "demo"
    root.mkdir()
    plane = ControlPlane(ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(root)},
        fabric=make_fabric(ScriptedProvider()), session_ttl=1.0))
    plane.start()
    make_repo(root)
    client = make_client(plane)
    with client:
        _, _, headers = login(client)
        assert client.get("/api/v1/tasks", headers=headers).status_code \
            == 200
        time.sleep(1.2)
        expired = client.get("/api/v1/tasks", headers=headers)
        assert expired.status_code == 401
    plane.close()


def test_session_creation_rate_limited(tmp_path):
    plane, client = _setup(tmp_path)
    with client:
        codes = []
        for index in range(25):
            response = client.post("/api/v1/sessions", json={
                "actor": f"user{index}", "project_id": "demo"})
            codes.append(response.status_code)
        assert 429 in codes
        limited = client.post("/api/v1/sessions", json={
            "actor": "one-more", "project_id": "demo"})
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
