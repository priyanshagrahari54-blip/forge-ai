"""Authenticated API tests (A81).

Every endpoint the contract requires — task creation, task status, task
cancellation, pause/resume, logs, results, project registration, and
server health — plus the authentication/authorization matrix (API keys,
sessions, roles, scopes) and structured error rendering.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_server import (  # noqa: E402
    TEST_TOKEN,
    ApprovalExecutor,
    auth_headers,
    create_task,
    http_status,
    make_client,
    make_server,
    success_executor,
    wait_for_status,
    wait_for_status_http,
)

from forge.server import TaskStatus  # noqa: E402

ADMIN = auth_headers(TEST_TOKEN)


def _viewer_headers(server, client) -> dict:
    response = client.post("/api/v1/auth/keys", headers=ADMIN,
                           json={"name": "viewer-key", "role": "viewer"})
    assert response.status_code == 200, response.text
    key = response.json()["key"]["key"]
    return auth_headers(key)


def _operator_headers(server, client, name: str = "op-key") -> dict:
    response = client.post("/api/v1/auth/keys", headers=ADMIN,
                           json={"name": name, "role": "operator"})
    assert response.status_code == 200, response.text
    return auth_headers(response.json()["key"]["key"])


# -- authentication ---------------------------------------------------------------

def test_ping_is_anonymous_and_minimal(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.get("/api/v1/ping")
            assert response.status_code == 200
            assert response.json() == {"ok": True,
                                       "server": "forge-server"}
    finally:
        server.close()


def test_every_protected_endpoint_requires_auth(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN)
            endpoints = [
                ("GET", "/api/v1/tasks"),
                ("GET", "/api/v1/tasks/%s" % task["task_id"]),
                ("GET", "/api/v1/tasks/%s/logs" % task["task_id"]),
                ("GET", "/api/v1/tasks/%s/result" % task["task_id"]),
                ("GET", "/api/v1/tasks/%s/events" % task["task_id"]),
                ("GET", "/api/v1/projects"),
                ("GET", "/api/v1/approvals"),
                ("GET", "/api/v1/notifications"),
                ("GET", "/api/v1/health"),
                ("GET", "/api/v1/status"),
                ("GET", "/api/v1/recovery"),
                ("GET", "/api/v1/policy"),
                ("GET", "/api/v1/auth/whoami"),
                ("POST", "/api/v1/tasks"),
            ]
            for method, url in endpoints:
                response = (client.get(url) if method == "GET"
                            else client.post(url, json={}))
                assert response.status_code == 401, (method, url)
                body = response.json()["error"]
                assert body["code"] == "AUTH_REQUIRED"
                assert body["request_id"]
    finally:
        server.close()


def test_unknown_and_malformed_tokens_fail_closed(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            for token in ("garbage", "fsk_doesnotexist",
                          "fss_doesnotexist", ""):
                response = client.get(
                    "/api/v1/status",
                    headers={"Authorization": "Bearer %s" % token})
                assert response.status_code == 401
            response = client.get(
                "/api/v1/status", headers={"Authorization": "Basic abc"})
            assert response.status_code == 401
    finally:
        server.close()


def test_whoami_reports_principal(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.get("/api/v1/auth/whoami", headers=ADMIN)
            principal = response.json()["principal"]
            assert principal["principal"] == "bootstrap"
            assert principal["role"] == "admin"
            assert principal["via"] == "bootstrap"
            assert "tasks:write" in principal["scopes"]
    finally:
        server.close()


# -- sessions -------------------------------------------------------------------------

def test_session_lifecycle_create_use_revoke(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.post("/api/v1/auth/sessions", headers=ADMIN)
            assert response.status_code == 200
            body = response.json()
            token = body["token"]
            assert token.startswith("fss_")
            session_headers = auth_headers(token)

            # The session works and inherits the principal (no escalation).
            who = client.get("/api/v1/auth/whoami",
                             headers=session_headers).json()["principal"]
            assert who["via"] == "session"
            assert who["role"] == "admin"

            create_task(client, session_headers, "via session")

            # Revoke → the session stops working immediately.
            session_id = body["session"]["session_id"]
            revoked = client.delete(
                "/api/v1/auth/sessions/%s" % session_id, headers=ADMIN)
            assert revoked.status_code == 200
            assert revoked.json()["revoked"] is True
            response = client.get("/api/v1/status", headers=session_headers)
            assert response.status_code == 401
    finally:
        server.close()


def test_sessions_survive_server_restart(tmp_path):
    db_path = tmp_path / "server" / "server.db"
    server = make_server(tmp_path, success_executor(), db_path=db_path)
    client = make_client(server)
    token = ""
    try:
        with client:
            token = client.post("/api/v1/auth/sessions",
                                headers=ADMIN).json()["token"]
    finally:
        server.close()

    restarted = make_server(tmp_path, success_executor(),
                            db_path=db_path, with_repo=False)
    client2 = make_client(restarted)
    try:
        with client2:
            response = client2.get("/api/v1/auth/whoami",
                                   headers=auth_headers(token))
            assert response.status_code == 200
            assert response.json()["principal"]["via"] == "session"
    finally:
        restarted.close()


# -- roles and scopes ---------------------------------------------------------------------

def test_viewer_can_read_but_not_write(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            viewer = _viewer_headers(server, client)
            create_task(client, ADMIN, "readable task")

            assert client.get("/api/v1/tasks",
                              headers=viewer).status_code == 200
            assert client.get("/api/v1/health",
                              headers=viewer).status_code == 200
            response = client.post(
                "/api/v1/tasks", headers=viewer,
                json={"project_id": "demo", "requirement": "not allowed"})
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "PERMISSION_DENIED"
            # Viewers cannot manage keys either.
            response = client.post(
                "/api/v1/auth/keys", headers=viewer,
                json={"name": "sneaky", "role": "admin"})
            assert response.status_code == 403
    finally:
        server.close()


def test_operator_runs_tasks_but_cannot_mint_keys(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            operator = _operator_headers(server, client)
            task = create_task(client, operator, "operator task")
            done = wait_for_status_http(client, operator,
                                        task["task_id"], {"completed"})
            assert done["status"] == "completed"
            response = client.post(
                "/api/v1/auth/keys", headers=operator,
                json={"name": "nope", "role": "viewer"})
            assert response.status_code == 403
    finally:
        server.close()


def test_admin_key_management(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            created = client.post(
                "/api/v1/auth/keys", headers=ADMIN,
                json={"name": "ci-bot", "role": "operator"})
            assert created.status_code == 200
            key = created.json()["key"]
            assert key["key"].startswith("fsk_")
            assert key["role"] == "operator"

            # Duplicate names and unknown roles are refused.
            duplicate = client.post(
                "/api/v1/auth/keys", headers=ADMIN,
                json={"name": "ci-bot", "role": "operator"})
            assert duplicate.status_code == 400
            bad_role = client.post(
                "/api/v1/auth/keys", headers=ADMIN,
                json={"name": "x", "role": "superuser"})
            assert bad_role.status_code == 400

            listing = client.get("/api/v1/auth/keys", headers=ADMIN)
            names = [item["name"] for item in listing.json()["keys"]]
            assert "ci-bot" in names
            assert all("key" not in item for item in listing.json()["keys"])

            # Revoked keys stop working.
            headers = auth_headers(key["key"])
            assert client.get("/api/v1/status",
                              headers=headers).status_code == 200
            revoked = client.delete("/api/v1/auth/keys/ci-bot",
                                    headers=ADMIN)
            assert revoked.json()["revoked"] is True
            assert client.get("/api/v1/status",
                              headers=headers).status_code == 401
    finally:
        server.close()


# -- task endpoints -------------------------------------------------------------------------

def test_task_crud_flow_over_http(tmp_path):
    server = make_server(tmp_path, success_executor("api"))
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "Add CSV export",
                               priority=5)
            task_id = task["task_id"]
            assert task["status"] in ("created", "queued")
            assert task["project_id"] == "demo"
            assert task["requirement"] == "Add CSV export"
            assert task["priority"] == 5

            done = wait_for_status_http(client, ADMIN, task_id,
                                        {"completed"})
            assert done["status"] == "completed"
            assert done["progress"] == 1.0
            assert done["checkpoint_id"]

            # Status endpoint includes the result for terminal tasks.
            response = client.get("/api/v1/tasks/%s" % task_id,
                                  headers=ADMIN)
            assert response.json()["task"]["result"]["summary"] == "api"

            listing = client.get("/api/v1/tasks?project_id=demo",
                                 headers=ADMIN).json()
            assert [item["task_id"] for item in listing["tasks"]] == \
                [task_id]
            assert listing["counts"]["completed"] == 1

            result = client.get("/api/v1/tasks/%s/result" % task_id,
                                headers=ADMIN).json()
            assert result["available"] is True
            assert result["result"]["summary"] == "api"

            logs = client.get("/api/v1/tasks/%s/logs" % task_id,
                              headers=ADMIN).json()
            messages = [entry["message"] for entry in logs["logs"]]
            assert any("Worker picked up task" in item
                       for item in messages)

            unknown = client.get("/api/v1/tasks/task-nope", headers=ADMIN)
            assert unknown.status_code == 404
            assert unknown.json()["error"]["code"] == "TASK_NOT_FOUND"
    finally:
        server.close()


def test_task_control_endpoints_pause_resume_cancel(tmp_path):
    server = make_server(tmp_path, success_executor(), start=False)
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "control me")
            task_id = task["task_id"]

            paused = client.post("/api/v1/tasks/%s/pause" % task_id,
                                 headers=ADMIN, json={})
            assert paused.status_code == 200
            assert paused.json()["task"]["status"] == "paused"

            resumed = client.post("/api/v1/tasks/%s/resume" % task_id,
                                  headers=ADMIN, json={})
            assert resumed.json()["task"]["status"] == "queued"

            cancelled = client.post("/api/v1/tasks/%s/cancel" % task_id,
                                    headers=ADMIN, json={})
            assert cancelled.json()["task"]["status"] == "cancelled"

            # Terminal: every control verb now conflicts (409).
            for verb in ("pause", "resume", "cancel"):
                response = client.post(
                    "/api/v1/tasks/%s/%s" % (task_id, verb),
                    headers=ADMIN, json={})
                assert response.status_code == 409, verb
                assert response.json()["error"]["code"] in (
                    "CONFLICT", "INVALID_TRANSITION")
    finally:
        server.close()


def test_expected_version_conflict_over_http(tmp_path):
    server = make_server(tmp_path, success_executor(), start=False)
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "versioned")
            response = client.post(
                "/api/v1/tasks/%s/cancel" % task["task_id"],
                headers=ADMIN, json={"expected_version": 999})
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "CONFLICT"
            response = client.post(
                "/api/v1/tasks/%s/cancel" % task["task_id"],
                headers=ADMIN, json={"expected_version": task["version"]})
            assert response.status_code == 200
            assert response.json()["task"]["status"] == "cancelled"
    finally:
        server.close()


def test_invalid_task_payloads_are_rejected(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            cases = [
                {"project_id": "demo", "requirement": ""},
                {"project_id": "demo", "requirement": "x" * 8001},
                {"project_id": "demo", "requirement": "x",
                 "priority": 10 ** 6},
                {"project_id": "demo", "requirement": "x",
                 "max_retries": 99},
                {"project_id": "demo"},
                {"requirement": "no project"},
                "not even a dict",
            ]
            for payload in cases:
                response = client.post("/api/v1/tasks", headers=ADMIN,
                                       json=payload)
                assert response.status_code == 400, payload
                assert response.json()["error"]["code"] == \
                    "INVALID_REQUEST"
            unknown_project = client.post(
                "/api/v1/tasks", headers=ADMIN,
                json={"project_id": "ghost", "requirement": "boo"})
            assert unknown_project.status_code == 404
            assert unknown_project.json()["error"]["code"] == \
                "PROJECT_NOT_FOUND"
    finally:
        server.close()


# -- projects -----------------------------------------------------------------------------

def test_project_registration_over_http(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    extra = tmp_path / "extra"
    extra.mkdir()
    try:
        with client:
            response = client.post(
                "/api/v1/projects", headers=ADMIN,
                json={"project_id": "extra", "root": str(extra),
                      "name": "Extra"})
            assert response.status_code == 200
            project = response.json()["project"]
            assert project["project_id"] == "extra"
            assert project["root"] == str(extra.resolve())

            listing = client.get("/api/v1/projects", headers=ADMIN).json()
            ids = [item["project_id"] for item in listing["projects"]]
            assert ids == ["demo", "extra"]

            # Tasks can now be submitted against the new project.
            task = create_task(client, ADMIN, "extra work",
                               project_id="extra")
            wait_for_status_http(client, ADMIN, task["task_id"],
                                 {"completed"})

            # Invalid registrations fail closed.
            bad = client.post(
                "/api/v1/projects", headers=ADMIN,
                json={"project_id": "evil",
                      "root": str(tmp_path / "missing")})
            assert bad.status_code == 400
            traversal = client.post(
                "/api/v1/projects", headers=ADMIN,
                json={"project_id": "../evil", "root": str(extra)})
            assert traversal.status_code == 400
            conflict = client.post(
                "/api/v1/projects", headers=ADMIN,
                json={"project_id": "demo", "root": str(extra)})
            assert conflict.status_code == 409
    finally:
        server.close()


# -- health / status ---------------------------------------------------------------------------

def test_health_and_status_endpoints(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            health = client.get("/api/v1/health", headers=ADMIN)
            assert health.status_code == 200
            body = health.json()
            assert body["status"] in ("ok", "degraded")
            assert body["components"]["database"]["ok"] is True
            assert body["components"]["workers"]["alive"] is True
            assert body["components"]["scheduler"]["running"] is True
            assert body["auth_mode"] == "local-dev"
            assert any("Local-development authentication" in warning
                       for warning in body["warnings"])

            status = client.get("/api/v1/status", headers=ADMIN)
            assert status.status_code == 200
            body = status.json()
            assert body["server"] == "forge-server"
            assert body["running"] is True
            assert body["workers"]["max"] >= 1
            assert "tasks" in body and "queue" in body
            assert body["profile"] == "assisted"
    finally:
        server.close()


def test_error_bodies_are_structured_and_safe(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.get("/api/v1/tasks/task-missing",
                                  headers=ADMIN)
            body = response.json()
            assert set(body) == {"error"}
            error = body["error"]
            assert set(error) >= {"code", "message", "request_id"}
            # No tracebacks or internal details leak.
            assert "Traceback" not in str(body)
            assert response.headers["X-Request-ID"]

            missing_route = client.get("/api/v1/nonexistent",
                                       headers=ADMIN)
            assert missing_route.status_code == 404
            assert missing_route.json()["error"]["code"] == "NOT_FOUND"
    finally:
        server.close()


def test_notifications_endpoints(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "notify me")
            wait_for_status_http(client, ADMIN, task["task_id"],
                                 {"completed"})
            listing = client.get("/api/v1/notifications",
                                 headers=ADMIN).json()
            assert listing["notifications"]
            notification = listing["notifications"][0]
            assert notification["unread"] is True

            read = client.post(
                "/api/v1/notifications/%s/read"
                % notification["notification_id"], headers=ADMIN)
            assert read.json()["marked"] is True

            unread = client.get("/api/v1/notifications?unread=1",
                                headers=ADMIN).json()["notifications"]
            assert all(item["unread"] for item in unread)

            all_read = client.post("/api/v1/notifications/read-all",
                                   headers=ADMIN,
                                   json={"project_id": "demo"})
            assert all_read.status_code == 200
            unread = client.get("/api/v1/notifications?unread=1",
                                headers=ADMIN).json()["notifications"]
            assert unread == []
    finally:
        server.close()
