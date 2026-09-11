"""Agent Creation Engine (A81): the Desktop Agent Manager.

The desktop app adds an Agent Manager: backend methods that expose the
engine per project, and a Tkinter window that is only a view — all
state changes go through the backend (and therefore the manager and the
lifecycle machine). Backend tests run against a real started
:class:`DesktopBackend`; window tests run under the stub-tkinter
harness used by the other desktop GUI tests.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_desktop_app_gui_stub import (  # noqa: E402
    _Widget,
    _install_stub_tkinter,
)

from forge.desktop_app.backend import BackendError, DesktopBackend  # noqa: E402


@pytest.fixture()
def backend(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "app.py").write_text("def health(): return True\n")
    app = DesktopBackend(actor="tester",
                         db_path=str(tmp_path / "desktop.db"))
    app.start({"demo": str(root)})
    yield app, root
    app.stop()


def test_agent_templates_available(backend):
    app, _root = backend
    templates = app.agent_templates()
    ids = [template["id"] for template in templates]
    assert ids == ["coding", "research", "security", "game-dev",
                   "os-dev", "documentation"]
    for template in templates:
        assert template["tools"] and template["benchmark"]


def test_agent_crud_and_lifecycle_through_backend(backend):
    app, root = backend
    assert app.list_agents("demo") == []
    created = app.create_agent("demo", "coder-1", template="coding")
    assert created["current_lifecycle"] == "validated"
    listed = app.list_agents("demo")
    assert [agent["name"] for agent in listed] == ["coder-1"]
    assert listed[0]["lifecycle"] == "validated"
    assert listed[0]["runnable"] is False

    with pytest.raises(BackendError):
        app.enable_agent("demo", "coder-1")  # untested

    tested = app.test_agent("demo", "coder-1")
    assert tested["current_lifecycle"] == "tested"
    enabled = app.enable_agent("demo", "coder-1")
    assert enabled["current_lifecycle"] == "enabled"
    assert app.list_agents("demo")[0]["runnable"] is True

    app.pause_agent("demo", "coder-1")
    app.resume_agent("demo", "coder-1")
    app.disable_agent("demo", "coder-1")
    assert app.show_agent("demo", "coder-1")["current_lifecycle"] == \
        "disabled"
    app.retire_agent("demo", "coder-1")
    with pytest.raises(BackendError):
        app.enable_agent("demo", "coder-1")


def test_backend_agent_errors_are_normalized(backend):
    app, _root = backend
    with pytest.raises(BackendError):
        app.show_agent("demo", "ghost")
    with pytest.raises(BackendError):
        app.list_agents("no-such-project")


def test_backend_agent_store_is_per_project(backend, tmp_path):
    app, root = backend
    other = tmp_path / "other"
    other.mkdir()
    app.add_project("other", str(other))
    app.create_agent("demo", "coder-1", template="coding")
    assert [agent["name"] for agent in app.list_agents("demo")] == \
        ["coder-1"]
    assert app.list_agents("other") == []
    # The store really lives inside the project's .forge directory.
    store = root / ".forge" / "agents"
    assert (store / "coder-1" / "current.json").exists()


def test_backend_export_agent_package(backend):
    app, _root = backend
    app.create_agent("demo", "coder-1", template="coding")
    package = app.export_agent("demo", "coder-1")
    assert package["agent"]["name"] == "coder-1"
    assert set(package["files"]) == {"agent.json", "spec.json",
                                     "permissions.lock"}


# ---------------------------------------------------------------------------
# Window (stub-tkinter)
# ---------------------------------------------------------------------------

class _FakeBackend:
    """Thread-safe fake with the agent surface the window needs."""

    def __init__(self):
        self.agents = [{
            "name": "coder-1", "version": 1, "lifecycle": "enabled",
            "current_version": 1, "current_lifecycle": "enabled",
            "runnable": True, "purpose": "code", "template": "coding",
            "tools": ["read_file"], "permissions": ["read_file"],
            "capabilities": ["coding"], "created_by": "tester",
        }]
        self.detail = {
            "agent": {"name": "coder-1", "version": 1,
                      "lifecycle": "enabled", "permissions": ["read_file"],
                      "template": "coding",
                      "spec": {"purpose": "code",
                               "capabilities": ["coding"],
                               "tools": ["read_file"],
                               "model": {"capabilities": ["coding"]},
                               "verification": {"benchmark": "coding"},
                               "limits": {"working_dirs": []}}},
            "current_version": 1, "current_lifecycle": "enabled",
            "runnable": True,
            "integrity": {"ok": True, "detail": "verified"},
            "benchmarks": {"benchmark": "coding", "score": 1.0,
                           "passed": 1, "total": 1,
                           "scenarios": [{"id": "x", "passed": True}]},
            "history": [],
        }
        self.calls: list[tuple] = []

    def projects(self):
        return [{"id": "demo"}]

    def list_agents(self, project_id):
        self.calls.append(("list", project_id))
        return list(self.agents)

    def show_agent(self, project_id, name):
        self.calls.append(("show", project_id, name))
        return dict(self.detail)

    def agent_templates(self):
        return [{"id": "coding", "description": "code", "purpose": "p",
                 "tools": ["read_file"], "benchmark": "coding"}]

    def create_agent(self, project_id, name, *, template="coding",
                     purpose=""):
        self.calls.append(("create", project_id, name, template))
        return {"current_lifecycle": "validated"}

    def validate_agent(self, project_id, name):
        self.calls.append(("validate", project_id, name))
        return {}

    def test_agent(self, project_id, name):
        self.calls.append(("test", project_id, name))
        return {}

    def enable_agent(self, project_id, name):
        self.calls.append(("enable", project_id, name))
        return {}

    def pause_agent(self, project_id, name):
        self.calls.append(("pause", project_id, name))
        return {}

    def resume_agent(self, project_id, name):
        self.calls.append(("resume", project_id, name))
        return {}

    def disable_agent(self, project_id, name):
        self.calls.append(("disable", project_id, name))
        return {}

    def retire_agent(self, project_id, name):
        self.calls.append(("retire", project_id, name))
        return {}


@pytest.fixture()
def gui(monkeypatch):
    return _install_stub_tkinter(monkeypatch)


def _drain(window):
    """Run the window's queue drain synchronously (stub has no event loop)."""
    for _ in range(10):
        try:
            window._drain()
        except Exception:
            break
        if window._queue.empty():
            break
        time.sleep(0.01)


def test_agent_manager_window_constructs_and_renders(gui):
    backend = _FakeBackend()
    window = gui.AgentManagerWindow(backend, None, "demo")
    window._render_agents()
    window._render_detail(backend.detail)
    window._set_status("ok")
    assert window._selected == ""
    window._on_close()
    assert window._stop.is_set()


def test_agent_manager_window_selection_and_actions(gui):
    backend = _FakeBackend()
    window = gui.AgentManagerWindow(backend, None, "demo")
    _drain(window)
    # Simulate listbox selection -> selection fetches detail.
    window._agent_list._returns["curselection"] = lambda: (0,)
    window._on_agent_selected()
    assert window._selected == "coder-1"
    # Every lifecycle action goes through the backend, never the manager.
    for method in ("validate_agent", "test_agent", "enable_agent",
                   "pause_agent", "resume_agent", "disable_agent",
                   "retire_agent"):
        window._invoke(method)
    _drain(window)
    names = [call[0] for call in backend.calls]
    for method in ("validate", "test", "enable", "pause", "resume",
                   "disable", "retire"):
        assert method in names, method
    window._on_close()


def test_agent_manager_window_create_dialog(gui):
    backend = _FakeBackend()
    window = gui.AgentManagerWindow(backend, None, "demo")
    window._open_create_dialog()
    window._show_templates()
    window._on_close()
    assert any(call[0] == "list" for call in backend.calls)


def test_agent_manager_window_without_backend_projects(gui):
    class _Empty:
        def projects(self):
            return []

        def list_agents(self, _pid):
            return []

        def agent_templates(self):
            return []

    window = gui.AgentManagerWindow(_Empty(), None, "")
    assert window._project.get() == ""
    window._render_agents()
    window._on_close()


def test_main_window_exposes_agent_manager(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    # Not running yet: the entry point must guard and return cleanly.
    app._show_agents()
    app._on_close()


def test_agent_manager_menu_registered(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    menubar = app.config(menu=app._menubar) if hasattr(app, "_menubar") else None
    del menubar  # widget-level menus are exercised at construction
    # The command must exist and be callable through the app.
    assert callable(app._show_agents)
    app._on_close()
