"""Agent Creation Engine (A81): control plane, desktop backend, UI contracts."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.control import ControlConfig, ControlPlane  # noqa: E402
from helpers_a34 import make_client, make_plane  # noqa: E402
from helpers_a81 import (  # noqa: E402
    GOOD_CHANGES,
    ScriptedAgentProvider,
    make_fabric,
    make_repo,
)

APP_SOURCE = Path(__file__).parent.parent / "forge" / "desktop_app" / "app.py"
BACKEND_SOURCE = (Path(__file__).parent.parent / "forge" / "desktop_app"
                  / "backend.py")


def _session(plane, client):
    from helpers_a34 import login

    with client:
        payload, _token, _headers = login(client)
    return plane.sessions.get(payload["session_id"])


def test_plane_agent_package_lifecycle(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       provider=ScriptedAgentProvider(GOOD_CHANGES))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    session = _session(plane, client)

    templates = plane.agent_package_templates(session)
    assert {item["template"] for item in templates["templates"]} == {
        "coding", "research", "security", "game-dev", "os-dev",
        "documentation", }

    created = plane.agent_package_create(
        session, {}, template="coding", name="plane-coder",
        purpose="plane-driven coding")
    assert created["name"] == "plane-coder"
    assert created["status"] == "created"
    assert created["specification"]["template"] == "coding"

    validated = plane.agent_package_validate(session, "plane-coder")
    assert validated["status"] == "validated"

    tested = plane.agent_package_test(session, "plane-coder")
    assert tested["advanced"] is True
    assert tested["benchmark"]["passed"] is True

    enabled = plane.agent_package_transition(session, "plane-coder",
                                             "enable")
    assert enabled["status"] == "enabled"

    run = plane.agent_package_run(session, "plane-coder",
                                  "add a notes module", approved=True)
    assert run["success"] is True
    assert run["changes"] == ["notes.py"]

    listing = plane.agent_packages(session)
    assert [item["name"] for item in listing["agents"]] == ["plane-coder"]

    versions = plane.agent_package_versions(session, "plane-coder")
    assert versions["current_version"] == 1

    paused = plane.agent_package_transition(session, "plane-coder", "pause")
    assert paused["status"] == "paused"
    disabled = plane.agent_package_transition(session, "plane-coder",
                                              "disable")
    assert disabled["status"] == "disabled"
    retired = plane.agent_package_transition(session, "plane-coder",
                                             "retire")
    assert retired["status"] == "retired"
    deleted = plane.agent_package_delete(session, "plane-coder")
    assert deleted["deleted"]["agent_id"]
    assert plane.agent_packages(session)["agents"] == []


def test_plane_engine_refuses_agent_actors_and_audits(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       provider=ScriptedAgentProvider(GOOD_CHANGES))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    session = _session(plane, client)
    plane.agent_package_create(session, {}, template="coding",
                               name="grabby")

    # The plane will not even mint agent-prefixed sessions: an actor
    # name with ':' is rejected at session creation.
    with pytest.raises(Exception, match="actor"):
        plane.create_session("agent:grabby", "demo")
    # A session opened under the agent's OWN name still cannot mutate
    # its package — self-reference is refused by the engine guard.
    agent_session, _token = plane.create_session("grabby", "demo")
    with pytest.raises(Exception, match="cannot create"):
        plane.agent_package_create(agent_session, {}, template="coding",
                                   name="clone")
    with pytest.raises(Exception, match="cannot|operator"):
        plane.agent_package_transition(agent_session, "grabby", "enable")
    with pytest.raises(Exception, match="cannot|operator"):
        plane.agent_package_update(agent_session, "grabby", {
            "name": "grabby", "purpose": "x",
            "capabilities": ["coding"], "tools": ["read_file"],
            "permissions": ["filesystem:read"]})
    # The denials are audited.
    audited = plane.audit.query(resource="agent-packages")
    assert any(event.operation == "enable"
               and event.decision.value == "DENY" for event in audited)
    # The package is untouched.
    assert plane.agent_package_get(session, "grabby")["status"] == "created"


def test_plane_engine_validation_errors(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    session = _session(plane, client)
    with pytest.raises(Exception):
        plane.agent_package_create(session, {}, template="nope",
                                   name="x-agent")
    with pytest.raises(Exception):
        plane.agent_package_create(session, {}, template="coding")
    with pytest.raises(Exception):
        plane.agent_package_create(session, {
            "name": "bad", "purpose": "x", "capabilities": ["zzz"],
            "tools": [], "permissions": []})
    with pytest.raises(Exception):
        plane.agent_package_transition(session, "ghost", "enable")


def test_desktop_backend_agent_manager(tmp_path):
    from forge.desktop_app.backend import BackendError, DesktopBackend

    fabric, _provider = make_fabric(GOOD_CHANGES)
    backend = DesktopBackend(actor="desktop-user", fabric=fabric)
    backend.start({"demo": str(make_repo(tmp_path / "desk"))})

    assert backend.list_agents("demo") == []
    assert [item["template"] for item in backend.agent_templates()] == [
        "coding", "research", "security", "game-dev", "os-dev",
        "documentation"]

    created = backend.create_agent("demo", "coding", "desk-coder",
                                   "desktop created coder")
    assert created["status"] == "created"
    assert backend.agent_action("demo", "desk-coder", "validate")[
        "status"] == "validated"
    tested = backend.agent_action("demo", "desk-coder", "test")
    assert tested["advanced"] is True
    assert tested["benchmark"]["passed"] is True
    assert backend.agent_action("demo", "desk-coder", "enable")[
        "status"] == "enabled"
    run = backend.run_agent("demo", "desk-coder", "add notes module",
                            approve=True)
    assert run["success"] is True
    assert run["changes"] == ["notes.py"]
    manifest = backend.get_agent("demo", "desk-coder")
    assert manifest["status"] == "enabled"
    assert [record["version"]
            for record in backend.agent_versions("demo", "desk-coder")] == [1]
    assert backend.agent_action("demo", "desk-coder", "disable")[
        "status"] == "disabled"
    assert [item["name"] for item in backend.list_agents("demo")] == [
        "desk-coder"]
    # Errors surface as BackendError with honest messages.
    with pytest.raises(BackendError):
        backend.get_agent("demo", "missing-agent")
    with pytest.raises(BackendError):
        backend.agent_action("demo", "desk-coder", "enable")  # disabled
    backend.stop()


def test_desktop_agent_manager_ui_contracts():
    """The Agents tab exists and wires every manager action (static)."""
    source = APP_SOURCE.read_text(encoding="utf-8")
    for anchor in (
            'self._notebook.add(agents_tab, text="Agents")',
            "def _agents_create(self)",
            "def _agents_action(self, action: str)",
            "def _agents_run_task(self",
            "def _agents_refresh(self)",
            "def _on_agent_selected(self",
            "def _show_agent_test(self",
            "def _show_agent_run(self",
            '"Validate", "validate"', '"Test", "test"',
            '"Enable", "enable"', '"Disable", "disable"',
            '"Pause", "pause"', '"Resume", "resume"',
            '"Retire", "retire"',
            '"coding", "research", "security", "game-dev"',
            '"os-dev", "documentation"'):
        assert anchor in source, anchor
    # Agent manager calls never run on the UI thread.
    assert "_agents_async" in source
    assert "threading.Thread(target=runner, daemon=True).start()" in source
    backend = BACKEND_SOURCE.read_text(encoding="utf-8")
    for method in ("def list_agents(", "def create_agent(",
                   "def agent_action(", "def run_agent(",
                   "def get_agent(", "def agent_versions(",
                   "def agent_templates("):
        assert method in backend, method


def test_engine_end_to_end_through_plane_persistence(tmp_path):
    """Packages persist in the project store across plane restarts."""
    root = make_repo(tmp_path / "repo")
    fabric, _provider = make_fabric(GOOD_CHANGES)

    def build(db_dir: Path) -> ControlPlane:
        plane = ControlPlane(ControlConfig(
            db_path=str(db_dir / "cockpit.db"),
            projects={"demo": str(root)}, fabric=fabric))
        plane.start()
        return plane

    plane = build(tmp_path / "db1")
    session, _token = plane.create_session("alice", "demo")
    plane.agent_package_create(session, {}, template="research",
                               name="persistent", purpose="survives")
    plane.agent_package_validate(session, "persistent")
    plane.stop()

    plane2 = build(tmp_path / "db2")
    session2, _token2 = plane2.create_session("bob", "demo")
    listing = plane2.agent_packages(session2)
    assert [item["name"] for item in listing["agents"]] == ["persistent"]
    assert listing["agents"][0]["status"] == "validated"
    plane2.stop()
