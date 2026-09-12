"""A81 desktop/cockpit visualization of agent activity.

Cockpit: the Agent Activity view (template hooks, route, renderer that
only talks to the execution endpoints, and live endpoints behind it).
Desktop: the polling snapshot carries the agent-activity section and
the GUI renders the per-role states.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import (login, make_client, make_plane, make_repo)  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy, PermissionRule, Resource)


@pytest.fixture()
def gui(monkeypatch):
    import test_desktop_app_gui_stub as gui_stub

    return gui_stub._install_stub_tkinter(monkeypatch)

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"

EXEC_HOOKS = ("exec-form", "exec-requirement", "exec-list", "exec-detail",
              "exec-refresh")

EXEC_ENDPOINTS = (
    "/api/v1/executions", "/api/v1/executions/approvals",
    "/api/v1/executions/cancel",
)


def _read(name: str) -> str:
    return (WEB / name).read_text()


def test_executions_template_hooks_present():
    html = _read("index.html")
    assert '<template id="tpl-executions">' in html
    for hook in EXEC_HOOKS:
        assert f'id="{hook}"' in html, hook
    assert 'href="#/executions"' in html
    assert 'data-route="executions"' in html


def test_executions_route_registered_with_renderer():
    js = _read("app.js")
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "executions" in routes_block
    assert "renderExecutions" in js
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Agent Activity" in palette
    assert "#/executions" in palette


def test_executions_renderer_only_talks_to_execution_endpoints():
    js = _read("app.js")
    renderer = js.split("function renderExecutions", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls, "renderer must call the API"
    for call in calls:
        assert call.startswith("/api/v1/executions"), call
    assert "/api/v1/executions" in calls


def test_executions_renderer_has_no_storage_or_credentials():
    js = _read("app.js")
    renderer = js.split("function renderExecutions", 1)[1].split(
        "\n/* ----------", 1)[0]
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
    html = _read("index.html")
    template = html.split('<template id="tpl-executions">', 1)[1].split(
        "</template>", 1)[0]
    for forbidden in ("onclick=", "onload=", "<script", "style="):
        assert forbidden not in template, forbidden


def _read_only_team():
    return [
        {"id": "research", "role": "researcher",
         "description": "Research the repository"},
        {"id": "plan", "role": "planner", "description": "Plan the work"},
        {"id": "security", "role": "security",
         "description": "Scan for secrets"},
        {"id": "docs", "role": "documentation", "description": "Document",
         "dependencies": ["research", "plan", "security"]},
    ]


def test_execution_endpoints_live_behind_renderer(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=allow_policy())
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/executions", headers=headers,
                                json={"requirement": "research and "
                                      "document the repository",
                                      "tasks": _read_only_team()})
        assert submitted.status_code == 200
        record = submitted.json()
        assert record["status"] == "QUEUED"
        execution_id = record["execution_id"]

        listed = client.get("/api/v1/executions", headers=headers)
        assert listed.status_code == 200
        assert any(r["execution_id"] == execution_id
                   for r in listed.json()["executions"])

        detail = client.get(
            f"/api/v1/executions/{execution_id}", headers=headers)
        assert detail.status_code == 200
        # poll to a terminal state
        deadline = time.time() + 90
        payload = detail.json()
        while payload["status"] not in ("SUCCEEDED", "FAILED", "PARTIAL",
                                        "CANCELLED") and \
                time.time() < deadline:
            payload = client.get(
                f"/api/v1/executions/{execution_id}", headers=headers).json()
            time.sleep(0.5)
        assert payload["status"] == "SUCCEEDED", payload.get("error")
        report = payload["report"]
        statuses = {t["id"]: t["status"] for t in report["tasks"]}
        assert set(statuses) == {"research", "plan", "security", "docs"}
        assert all(s == "SUCCEEDED" for s in statuses.values())
        # the agent-activity visualization payload is present
        activity = payload["activity"]
        agents = {a["role"]: a for a in activity["agents"]}
        assert "researcher" in agents and "documentation" in agents
        assert agents["researcher"]["state"] == "succeeded"
        assert report["messages"]
        assert report["events"]
        # approvals endpoint responds (none pending for read-only work)
        approvals = client.get(
            f"/api/v1/executions/{execution_id}/approvals", headers=headers)
        assert approvals.status_code == 200
        assert approvals.json()["approvals"] == []
        # unknown execution -> 404
        assert client.get(
            "/api/v1/executions/does-not-exist", headers=headers
        ).status_code == 404


def test_invalid_execution_submissions_rejected(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        def post(body):
            return client.post("/api/v1/executions", headers=headers,
                               json=body)
        # the API normalizes request-validation errors to 400
        assert post({"requirement": "x",
                     "tasks": []}).status_code == 400
        assert post({"requirement": "x",
                     "tasks": [{"role": "wizard",
                                "description": "d"}]}).status_code == 400
        assert post({"requirement": "x",
                     "tasks": [{"role": "coder", "description": "d",
                                "writes": ["/etc/passwd"]}]}
                    ).status_code == 400
        assert post({"requirement": "x",
                     "tasks": [{"role": "coder", "description": "d",
                                "writes": ["../escape.py"]}]}
                    ).status_code == 400
        # cycle: a -> b -> a (b declared before a references it)
        assert post({"requirement": "x", "tasks": [
            {"id": "a", "role": "planner", "description": "d",
             "dependencies": ["b"]},
            {"id": "b", "role": "coder", "description": "d",
             "dependencies": ["a"]},
        ]}).status_code == 400
        # duplicate ids
        assert post({"requirement": "x", "tasks": [
            {"id": "a", "role": "planner", "description": "d"},
            {"id": "a", "role": "coder", "description": "d"},
        ]}).status_code == 400


def test_cancel_execution_endpoint(tmp_path):
    """The coder waits on a write approval, so the run is deterministically
    in-flight when the operator cancels."""
    plane = make_plane(tmp_path, start=True, approval_timeout=30.0)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/executions", headers=headers,
                                json={"requirement": "add csv export",
                                      "tasks": [
                                          {"id": "code", "role": "coder",
                                           "description": "add csv export",
                                           "writes": ["app.py",
                                                      "tests/test_csv.py"]}]})
        assert submitted.status_code == 200
        execution_id = submitted.json()["execution_id"]
        # wait until it is running (control registered)
        deadline = time.time() + 15
        cancelled = None
        while time.time() < deadline:
            resp = client.post(
                f"/api/v1/executions/{execution_id}/cancel",
                headers=headers)
            if resp.status_code == 200:
                cancelled = resp.json()
                break
            time.sleep(0.2)
        assert cancelled is not None, "cancel was never accepted"
        deadline = time.time() + 30
        payload = {}
        while time.time() < deadline:
            payload = client.get(
                f"/api/v1/executions/{execution_id}", headers=headers).json()
            if payload["status"] in ("SUCCEEDED", "FAILED", "PARTIAL",
                                     "CANCELLED"):
                break
            time.sleep(0.3)
        assert payload["status"] == "CANCELLED"
        # A32 atomicity: the cancelled run left no partial change set
        app_py = (Path(plane.projects["demo"].root) / "app.py").read_text()
        assert app_py == "def health(): return True\n"


def test_cross_session_executions_not_visible(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        submitted = client.post("/api/v1/executions", headers=headers,
                                json={"requirement": "x",
                                      "tasks": [
                                          {"id": "a", "role": "planner",
                                           "description": "d"}]})
        execution_id = submitted.json()["execution_id"]
        # a different actor cannot see the record
        _s2, _t2, headers2 = login(client, actor="mallory")
        assert client.get(
            f"/api/v1/executions/{execution_id}", headers=headers2
        ).status_code == 404
        assert all(r["execution_id"] != execution_id
                   for r in client.get(
                       "/api/v1/executions", headers=headers2
                   ).json()["executions"])


# ------------------------------------------------ desktop app (A81) -------

def test_backend_snapshot_carries_agent_activity(tmp_path):
    from forge.desktop_app.backend import DesktopBackend

    root = tmp_path / "proj"
    root.mkdir()
    make_repo(root)
    backend = DesktopBackend(actor="tester",
                             db_path=str(tmp_path / "desktop.db"),
                             approval_timeout=30.0)
    backend.start({"demo": str(root)})
    try:
        snapshot = backend.poll_snapshot("demo")
        assert "agent_activity" in snapshot
        # no execution yet: honestly absent
        assert snapshot["agent_activity"].get("present") is False
        # submit an execution through the plane and see it in the snapshot
        plane = backend._require_plane()
        session = plane.sessions.create(
            actor="tester", project_id="demo")[0]
        record = plane.submit_execution(
            session, "research the repo", [
                {"id": "research", "role": "researcher",
                 "description": "Research the repository"}])
        deadline = time.time() + 60
        while time.time() < deadline:
            latest = plane.executions.get(record.id)
            if latest.status.value in ("SUCCEEDED", "FAILED", "PARTIAL",
                                       "CANCELLED"):
                break
            time.sleep(0.3)
        snapshot = backend.poll_snapshot("demo")
        activity = snapshot["agent_activity"]
        assert activity.get("present") is True
        assert activity["execution_id"] == record.id
        assert activity["status"] in ("SUCCEEDED", "FAILED", "PARTIAL",
                                      "CANCELLED")
        agents = {a["role"]: a for a in
                  activity["activity"]["agents"]}
        assert "researcher" in agents
    finally:
        backend.stop()


def test_backend_snapshot_isolates_agent_activity_errors(tmp_path):
    """A backend without a running plane still returns a well-formed
    snapshot: the section failure is captured, never raised."""
    from forge.desktop_app.backend import DesktopBackend

    backend = DesktopBackend(actor="tester",
                             db_path=str(tmp_path / "d.db"))
    snapshot = backend.poll_snapshot("missing-project")
    assert "agent_activity" in snapshot
    assert snapshot["agent_activity"] == {}
    assert "agent_activity" in snapshot["errors"]


def test_gui_renders_agent_activity_panel(gui):
    from forge.desktop_app.backend import DesktopBackend

    app = gui.ForgeDesktopApp(DesktopBackend())
    app._current_project.set("demo")
    # absent case
    app._apply_snapshot({
        "project_id": "demo", "tasks": [], "approvals": [],
        "agent_activity": {"present": False},
        "selected": None, "events": [], "event_cursor": 0, "errors": {}})
    assert "No parallel execution" in "\n".join(app._agent_lines)
    # present case: per-role states, locks, and counters render
    app._apply_snapshot({
        "project_id": "demo", "tasks": [], "approvals": [],
        "agent_activity": {
            "present": True, "execution_id": "abc12345deadbeef",
            "status": "RUNNING", "requirement": "build the thing",
            "activity": {
                "agents": [
                    {"role": "coder", "state": "running",
                     "task_id": "t1", "attempts": 1,
                     "held_locks": ["src/a.py"]},
                    {"role": "tester", "state": "blocked",
                     "task_id": "t2", "attempts": 0,
                     "held_locks": [],
                     "blocked_reason": "dependency t1 ended FAILED"},
                ],
                "counts": {"running": 1, "blocked": 1},
            },
        },
        "selected": None, "events": [], "event_cursor": 0, "errors": {}})
    content = "\n".join(app._agent_lines)
    assert "coder" in content and "running" in content
    assert "tester" in content and "blocked" in content
    assert "src/a.py" in content
    assert "dependency t1 ended FAILED" in content
    assert "running=1" in content
    app._on_close()


def allow_policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="agents-ok", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="reads-ok", resource=Resource.FILESYSTEM,
                       operation="read", scope="**", effect="ALLOW"),
        PermissionRule(id="writes-ok", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])
