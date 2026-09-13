"""A82 — the desktop Agent Manager.

Two layers, both exercised headlessly:

* :class:`DesktopBackend` — the GUI-free API the window calls. It must go
  through the same engine as the CLI, with the same actor, so a desktop
  action is validated, lifecycle-gated, and recorded exactly like a CLI
  one.
* The Tk window itself, driven through the stub-tkinter harness the
  existing desktop tests use, so widget code is executed (no display).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a82 import make_agent_fabric, make_project  # noqa: E402

from forge.desktop_app.backend import BackendError, DesktopBackend  # noqa: E402


@pytest.fixture()
def repo(tmp_path):
    return make_project(tmp_path / "demo")


@pytest.fixture()
def backend(repo, tmp_path):
    app = DesktopBackend(actor="alice",
                         db_path=str(tmp_path / "desktop.db"),
                         fabric=make_agent_fabric(),
                         approval_timeout=30.0)
    app.start({"demo": str(repo)})
    yield app
    app.stop()


# -- backend API ---------------------------------------------------------


def test_summary_lists_templates_and_agents(backend):
    state = backend.agent_summary("demo")
    assert state["counts"]["total"] == 0
    assert len(state["templates"]) == 6
    assert state["runnable_states"] == ["enabled"]
    backend.create_agent("demo", "exporter", template="coding")
    state = backend.agent_summary("demo")
    assert state["counts"]["total"] == 1
    assert state["counts"]["by_state"] == {"created": 1}
    assert state["agents"][0]["name"] == "exporter"


def test_create_validate_grant_test_enable_flow(backend):
    created = backend.create_agent("demo", "exporter", template="coding")
    assert created["state"] == "created"
    assert created["template"] == "coding"

    report = backend.validate_agent("demo", "exporter")
    assert report["passed"] is True
    assert backend.agent_detail("demo", "exporter")["summary"]["state"] == \
        "validated"

    granted = backend.grant_agent_spec("demo", "exporter")
    assert {item["operation"] for item in granted} == {
        "read_file", "search_files", "git_status", "run_tests", "write_file"}

    benchmark = backend.test_agent("demo", "exporter")
    assert benchmark["passed"] is True

    result = backend.set_agent_state("demo", "exporter", "enabled")
    assert result["state"] == "enabled"

    detail = backend.agent_detail("demo", "exporter")
    assert detail["summary"]["state"] == "enabled"
    assert detail["benchmark"]["passed"] is True
    assert "disabled" in detail["allowed_transitions"]
    assert detail["permissions"]["granted"]


def test_desktop_records_the_desktop_actor(backend):
    backend.create_agent("demo", "exporter", template="coding")
    detail = backend.agent_detail("demo", "exporter")
    assert detail["summary"]["created_by"] == "alice"
    backend.validate_agent("demo", "exporter")
    history = detail_lifecycle(backend, "exporter")
    assert history[-1]["actor"] == "alice"


def detail_lifecycle(backend, name):
    return backend.agent_detail("demo", name)["lifecycle"]["history"]


def test_desktop_cannot_skip_the_lifecycle(backend):
    backend.create_agent("demo", "exporter", template="coding")
    with pytest.raises(BackendError):
        backend.set_agent_state("demo", "exporter", "enabled")
    with pytest.raises(BackendError):
        backend.test_agent("demo", "exporter")
    assert backend.agent_detail("demo", "exporter")["summary"]["state"] == \
        "created"


def test_desktop_cannot_self_grant(backend):
    backend.create_agent("demo", "exporter", template="coding")
    engine = backend._agent_engine("demo")
    with pytest.raises(Exception) as info:
        engine.grant("exporter", "write_file", actor="exporter")
    assert "itself" in str(info.value)
    assert backend.agent_detail("demo", "exporter")["permissions"][
        "granted"] == []


def test_desktop_refuses_an_unknown_template(backend):
    with pytest.raises(BackendError) as info:
        backend.create_agent("demo", "exporter", template="skynet")
    assert "Unknown template" in str(info.value)


def test_desktop_requires_a_template(backend):
    with pytest.raises(BackendError):
        backend.create_agent("demo", "exporter", template="")


def test_desktop_refuses_an_unknown_agent(backend):
    with pytest.raises(BackendError):
        backend.agent_detail("demo", "ghost")
    with pytest.raises(BackendError):
        backend.validate_agent("demo", "ghost")


def test_desktop_refuses_an_unknown_project(backend):
    with pytest.raises(BackendError):
        backend.agent_summary("nope")


def test_desktop_revokes_a_permission(backend):
    backend.create_agent("demo", "exporter", template="coding")
    backend.grant_agent_spec("demo", "exporter")
    backend.revoke_agent_permission("demo", "exporter", "write_file")
    permissions = backend.agent_detail("demo", "exporter")["permissions"]
    assert "write_file" not in permissions["granted"]
    assert "write_file" in permissions["not_granted"]


def test_desktop_run_uses_the_same_engine(backend, repo):
    from helpers_a82 import AgentProvider, action_payload, write_action

    backend.create_agent("demo", "exporter", template="coding")
    backend.validate_agent("demo", "exporter")
    backend.grant_agent_spec("demo", "exporter")
    backend.test_agent("demo", "exporter")
    backend.set_agent_state("demo", "exporter", "enabled")
    engine = backend._agent_engine("demo")
    engine.runtime.fabric = make_agent_fabric(AgentProvider(action_payload(
        actions=[write_action("export.py",
                              "def export_csv():\n    return 'x'\n")])))
    result = backend.run_agent("demo", "exporter", "add CSV export",
                               approved=True)
    assert result["success"] is True, result
    assert (Path(repo) / "export.py").is_file()
    detail = backend.agent_detail("demo", "exporter")
    assert detail["summary"]["run_count"] == 1
    assert detail["history"][-1]["success"] is True


def test_agent_engines_are_cached_per_project_root(backend, tmp_path):
    other = tmp_path / "second"
    make_project(other)
    backend.add_project("second", str(other))
    first = backend._agent_engine("demo")
    assert backend._agent_engine("demo") is first
    assert backend._agent_engine("second") is not first
    backend.create_agent("demo", "exporter", template="coding")
    assert backend.agent_summary("second")["counts"]["total"] == 0
    assert backend.agent_summary("demo")["counts"]["total"] == 1


# -- the Tk window (stub tkinter) ---------------------------------------


class _Widget:
    def __init__(self, *args, **kwargs):
        self._returns: dict = {}
        self._config = dict(kwargs)
        self._destroyed = False

    def __getattr__(self, item):
        returns = self.__dict__.get("_returns", {})
        if item in returns:
            value = returns[item]
            return value if callable(value) else (lambda *a, **k: value)

        def _call(*args, **kwargs):
            if item == "get" and not args:
                return ""
            if item == "curselection":
                return ()
            if item == "destroy":
                self._destroyed = True
            return None

        return _call

    def __setitem__(self, key, value):
        self._config[key] = value

    def __getitem__(self, key):
        return self._config.get(key)


class _Var:
    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


def _install_stub_tkinter(monkeypatch):
    tk = types.ModuleType("tkinter")
    for widget in ("Tk", "Toplevel", "Menu", "Text", "Listbox", "Label",
                   "StringVar"):
        setattr(tk, widget, _Var if widget == "StringVar" else _Widget)
    tk.TclError = type("TclError", (Exception,), {})

    def _const(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return name.lower()

    tk.__getattr__ = _const  # type: ignore[attr-defined]
    ttk = types.ModuleType("tkinter.ttk")
    for widget in ("Frame", "Label", "Button", "Combobox", "PanedWindow",
                   "Notebook", "Scrollbar", "LabelFrame", "Entry"):
        setattr(ttk, widget, _Widget)
    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askdirectory = lambda **k: ""
    messagebox = types.ModuleType("tkinter.messagebox")
    notices = []
    messagebox.showerror = lambda *a, **k: notices.append(("error", a))
    messagebox.showinfo = lambda *a, **k: notices.append(("info", a))
    messagebox.askyesno = lambda *a, **k: False
    tk.ttk = ttk  # type: ignore[attr-defined]
    tk.filedialog = filedialog  # type: ignore[attr-defined]
    tk.messagebox = messagebox  # type: ignore[attr-defined]
    sys.modules.pop("forge.desktop_app.app", None)
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", filedialog)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", messagebox)
    import forge.desktop_app.app as app_module

    app_module._test_notices = notices
    return app_module


@pytest.fixture()
def gui(monkeypatch):
    return _install_stub_tkinter(monkeypatch)


def _summary():
    return {"root": "/tmp/demo",
            "agents": [{"name": "exporter", "state": "enabled",
                        "version": "1.0.0", "role": "coding",
                        "template": "coding", "purpose": "exports",
                        "capabilities": ["coding"],
                        "tools": ["read_file", "write_file"],
                        "operations": ["read_file", "write_file"],
                        "mode_ceiling": "assisted",
                        "memory_scope": "agent", "run_count": 2,
                        "created_by": "alice", "updated_at": 0.0,
                        "fingerprint": "abc", "benchmark_passed": True}],
            "templates": [{"id": "coding", "title": "Coding Agent",
                           "description": "d", "role": "coding",
                           "capabilities": ["coding"],
                           "tools": ["read_file"],
                           "operations": ["read_file"],
                           "mode_ceiling": "assisted",
                           "allowed_paths": [], "denied_paths": [],
                           "verification": {}, "limits": {}}],
            "counts": {"total": 1, "by_state": {"enabled": 1}},
            "states": ["created"], "runnable_states": ["enabled"]}


def test_agent_manager_window_builds_and_renders(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    app._show_agent_manager_window(_summary())
    assert app._agent_names == ["exporter"]
    # Reusing the same window on a refresh must not rebuild it.
    window = app._agent_window
    app._show_agent_manager_window(_summary())
    assert app._agent_window is window
    app._show_agent_manager_async()  # backend not running -> info dialog
    app._on_close()


def test_agent_detail_rendering_covers_every_section(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    app._show_agent_manager_window(_summary())
    detail = {
        "summary": _summary()["agents"][0],
        "spec": {"memory": {"scope": "agent"}},
        "lifecycle": {"history": [{"from": "tested", "to": "enabled",
                                   "actor": "alice", "reason": "ready"}]},
        "allowed_transitions": ["paused", "disabled"],
        "permissions": {"granted": ["read_file"],
                        "not_granted": ["write_file"],
                        "mode_ceiling": "assisted",
                        "allowed_paths": [], "denied_paths": ["secrets/"],
                        "blocked_always": ["expose_secrets"]},
        "grants": {"grants": [], "revocations": [], "refusals": []},
        "versions": [{"version": "1.0.0", "fingerprint": "abcdef123456",
                      "notes": "initial"}],
        "benchmark": {"passed": True, "reason": "all passed",
                      "scenarios": [{"name": "spec-integrity",
                                     "status": "passed"}]},
        "history": [{"run_id": "r1", "success": True, "stage": "complete",
                     "error": ""}],
        "memory": {"scope": "agent", "private_entries": 1,
                   "max_entries": 64},
        "usage": {"runs_last_hour": 1, "max_runs_per_hour": 20,
                  "active_runs": 0},
        "directory": ".forge/agents/exporter",
    }
    app._show_agent_detail(detail)
    app._show_agent_detail({**detail, "benchmark": {}, "history": []})
    app._on_close()


def test_agent_buttons_require_a_selection(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    app._show_agent_manager_window(_summary())
    app._agent_list._returns["curselection"] = lambda: ()
    app._create_agent()          # no name typed yet
    app._agent_action("validate")
    app._agent_action("test")
    app._agent_set_state("enabled")
    app._grant_agent_spec()
    notices = gui._test_notices
    assert len(notices) == 5
    assert all(kind == "info" for kind, _args in notices)
    assert "Enter an agent name" in notices[0][1][1]
    assert "Select an agent" in notices[1][1][1]
    app._on_close()


def test_selected_agent_maps_to_the_list_index(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    app._show_agent_manager_window(_summary())
    app._agent_list._returns["curselection"] = lambda: (0,)
    assert app._selected_agent() == "exporter"
    app._agent_list._returns["curselection"] = lambda: (9,)
    assert app._selected_agent() == ""
    app._on_close()


def test_agent_actions_dispatch_to_the_backend(gui, monkeypatch):
    app = gui.ForgeDesktopApp(DesktopBackend())
    calls = []

    class Recorder:
        running = True

        def agent_summary(self, project_id):
            calls.append(("summary", project_id))
            return _summary()

        def agent_detail(self, project_id, name):
            calls.append(("detail", name))
            return {"summary": _summary()["agents"][0]}

        def create_agent(self, project_id, name, **kwargs):
            calls.append(("create", name, kwargs["template"]))
            return _summary()["agents"][0]

        def validate_agent(self, project_id, name):
            calls.append(("validate", name))
            return {"passed": True}

        def test_agent(self, project_id, name):
            calls.append(("test", name))
            return {"passed": False, "reason": "boundary failed"}

        def set_agent_state(self, project_id, name, state):
            calls.append(("state", name, state))
            return {"state": state}

        def grant_agent_spec(self, project_id, name):
            calls.append(("grant", name))
            return [{"operation": "read_file"}]

    app.backend = Recorder()
    app._current_project.set("demo")
    app._show_agent_manager_window(_summary())
    app._agent_list._returns["curselection"] = lambda: (0,)
    # The stub Entry has no real text buffer, so hand back the typed values;
    # the template combobox is backed by a StringVar, which does hold one.
    app._agent_name._returns["get"] = lambda: "scribe"
    app._agent_purpose._returns["get"] = lambda: "writes docs"
    app._agent_template.set("documentation")
    assert app._agent_template.get() == "documentation"

    import threading

    real_thread = threading.Thread

    class SyncThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target

        def start(self):
            self.target()

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(threading, "Thread", SyncThread)
    try:
        app._create_agent()
        app._agent_action("validate")
        app._agent_action("test")
        app._agent_set_state("enabled")
        app._grant_agent_spec()
        app._on_agent_selected()
    finally:
        monkeypatch.setattr(threading, "Thread", real_thread)

    kinds = [call[0] for call in calls]
    assert kinds.count("create") == 1
    assert ("create", "scribe", "documentation") in calls
    assert ("validate", "exporter") in calls
    assert ("test", "exporter") in calls
    assert ("state", "exporter", "enabled") in calls
    assert ("grant", "exporter") in calls
    assert ("detail", "exporter") in calls

    # The queue received a renderable payload for each action.
    queued = []
    while not app._queue.empty():
        queued.append(app._queue.get_nowait()[0])
    assert queued.count("agents") >= 5
    assert "agent_detail" in queued
    app._on_close()


def test_agent_manager_reports_backend_errors(gui, monkeypatch):
    app = gui.ForgeDesktopApp(DesktopBackend())

    class Broken:
        running = True

        def agent_summary(self, project_id):
            raise BackendError("nope")

    app.backend = Broken()
    app._current_project.set("demo")

    import threading

    class SyncThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target

        def start(self):
            self.target()

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(threading, "Thread", SyncThread)
    try:
        app._show_agent_manager_async()
    finally:
        monkeypatch.setattr(threading, "Thread", threading.Thread)
    kind, payload = app._queue.get_nowait()
    assert kind == "error"
    assert payload == "nope"
    app._on_close()
