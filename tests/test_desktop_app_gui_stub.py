"""Stub-tkinter logic tests for the desktop GUI.

There is no display (or tkinter) in CI, so these tests install a minimal
fake ``tkinter`` package into ``sys.modules`` and drive the real
:class:`ForgeDesktopApp` methods: construction, snapshot rendering, event
appending, approvals, dialogs, and the ``main()``/``launch()`` entry
points. This catches NameErrors, typos, and wrong-arity calls in widget
code. It does not validate real Tk semantics.
"""
from __future__ import annotations

import sys
import types

import pytest

from forge.desktop_app.backend import DesktopBackend


class _Widget:
    def __init__(self, *args, **kwargs):
        self._returns: dict = {}
        self._config = dict(kwargs)
        self._packed = False
        self._destroyed = False

    def __getattr__(self, item):
        returns = self.__dict__.get("_returns", {})
        if item in returns:
            value = returns[item]
            if callable(value):
                return value
            return lambda *a, **k: value
        config = self.__dict__.get("_config", {})

        def _call(*args, **kwargs):
            if item == "get" and not args:
                return ""
            if item == "curselection":
                return ()
            if item == "winfo_children":
                return []
            if item == "cget" and args:
                return config.get(args[0], "")
            if item == "pack":
                self._packed = True
                return None
            if item == "pack_forget":
                self._packed = False
                return None
            if item == "destroy":
                self._destroyed = True
                return None
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
    tk.Tk = _Widget
    tk.Toplevel = _Widget
    tk.Menu = _Widget
    tk.Text = _Widget
    tk.Listbox = _Widget
    tk.Label = _Widget
    tk.StringVar = _Var
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
    messagebox.showerror = lambda *a, **k: None
    messagebox.showinfo = lambda *a, **k: None
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

    return app_module


@pytest.fixture()
def gui(monkeypatch):
    return _install_stub_tkinter(monkeypatch)


def _task(task_id="t-abc123", status="RUNNING"):
    return {"id": task_id, "project_id": "demo",
            "requirement": "add csv export", "status": status,
            "stage": "coding", "mode": "assisted", "actor": "tester",
            "model": "m/a34", "provider": "p", "error": "",
            "rollback": False, "files": ["app.py"], "version": 1,
            "created_at": 0.0, "started_at": 0.0, "finished_at": None}


def test_constructs_without_projects(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    assert app.title is not None
    app._on_close()


def test_first_run_declines_to_setup(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    app._first_run()  # askyesno -> False -> setup dialog path
    app._show_about()
    app._on_close()


def test_snapshot_rendering_paths(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    app._current_project.set("demo")
    snapshot = {
        "project_id": "demo",
        "tasks": [_task("t-1", "RUNNING"), _task("t-2", "QUEUED")],
        "approvals": [{"id": "a-1", "label": "write app.py",
                       "reason": "low-risk write", "operation": "write"}],
        "selected": _task("t-1", "RUNNING"),
        "events": [{"seq": 1, "event_type": "task.started",
                    "data": {"mode": "assisted"}},
                   {"seq": 2, "event_type": "stage_started",
                    "data": '{"stage": "coding}'},
                   {"seq": 3, "event_type": "note", "data": {}}],
        "event_cursor": 3,
        "errors": {},
    }
    app._apply_snapshot(snapshot)
    # Same membership refresh + failed selection rendering.
    app._apply_snapshot({**snapshot, "selected": None, "events": []})
    failed = _task("t-1", "FAILED")
    failed["error"] = "No working code model is available."
    app._render_selected(failed)
    succeeded = _task("t-2", "SUCCEEDED")
    app._render_selected(succeeded)
    app._render_approvals([])
    app._on_close()


def test_task_selection_resets_log(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    app._task_ids = ["t-1", "t-2"]
    app._task_list._returns["curselection"] = lambda: (1,)
    app._on_task_selected()
    assert app._selected_task == "t-2"
    assert app._event_cursor == 0
    app._on_close()


def test_doctor_and_models_windows(gui):
    app = gui.ForgeDesktopApp(DesktopBackend())
    app._readiness = {"ready": False, "usable_models": [],
                      "checks": [{"name": "ollama", "ok": False,
                                  "detail": "not reachable",
                                  "remediation": "ollama serve"}]}
    app._render_readiness_pill()
    app._show_doctor_window(app._readiness)
    app._readiness = {"ready": True, "usable_models": ["m/a34"], "checks": []}
    app._render_readiness_pill()
    app._show_doctor_window(app._readiness)
    app._show_models_window({"models": [{"name": "m/a34", "provider": "p",
                                         "free": True, "local": True}],
                             "providers": [{"name": "p", "kind": "mock",
                                            "model": ""}],
                             "health": {"m/a34": "ok"}})
    app._on_close()


def test_main_rejects_bad_project_spec(gui):
    with pytest.raises(SystemExit) as exc:
        gui.main(["--project", "oops-no-equals"])
    assert exc.value.code == 2


def test_launch_starts_and_stops_backend(gui, tmp_path, monkeypatch):
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    backends = []
    real_init = gui.ForgeDesktopApp.__init__

    def tracking_init(self, backend, initial_projects=None):
        backends.append(backend)
        real_init(self, backend, initial_projects)

    monkeypatch.setattr(gui.ForgeDesktopApp, "__init__", tracking_init)
    monkeypatch.setattr(gui.ForgeDesktopApp, "mainloop",
                        lambda self: self._on_close(), raising=False)
    gui.launch({"demo": str(repo)},
               db_path=str(tmp_path / "desktop.db"))
    assert len(backends) == 1
    assert backends[0].running is False
    assert (tmp_path / "desktop.db").exists()


def test_memory_viewer_tab_methods(gui):
    """The Memory tab's refresh/search/select/delete paths run headless."""
    app = gui.ForgeDesktopApp(DesktopBackend())
    # No backend running yet: refresh shows the placeholder, never raises.
    app._refresh_memory()
    app._search_memory()
    # No selection: the handlers return without error.
    app._on_memory_selected()
    app._delete_selected_memory()
    app._on_close()


def test_slug_and_short_helpers(gui):
    assert gui._slug("My Project!") == "my-project"
    assert gui._slug("") == "project"
    assert gui._short("t-" + "a" * 30).endswith("..")
    assert gui._short("t-1") == "t-1"
