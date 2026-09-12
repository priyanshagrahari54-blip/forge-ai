"""A81 desktop app integration: the backend in LOCAL/SERVER/HYBRID modes.

Headless backend tests (no GUI) plus stub-tkinter tests for the Server
tab rendering paths.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from forge.desktop_app.backend import BackendError, DesktopBackend
from helpers_link import LinkEnv, wait_until


@pytest.fixture()
def linked(tmp_path):
    """A DesktopBackend + a server env, registered and configured."""
    env = LinkEnv(tmp_path / "server")
    env.make_repo()
    secret = env.register()
    repo_root = Path(env.plane.projects["demo"].root)

    backend = DesktopBackend(link_config_dir=str(tmp_path / "client"))
    backend.start({"demo": str(repo_root)})

    settings = {
        "server_url": "http://forge-server:8000",
        "client_id": "g560",
        "project_id": "demo",
        "mode": "server",
        "secret": secret,
        "config_dir": str(tmp_path / "client"),
        # Route the client's signed requests into the in-process server.
        "_sender": env.sender(),
    }
    yield backend, env, settings, repo_root
    backend.stop()
    env.close()


# -- configuration ----------------------------------------------------------------

def test_link_configure_validates_and_persists(linked, tmp_path):
    backend, env, settings, _root = linked
    status = backend.link_configure(settings)
    assert status["configured"] is True
    assert status["client_id"] == "g560"
    saved = Path(status["settings_path"])
    assert saved.exists()
    raw = saved.read_text(encoding="utf-8")
    assert settings["secret"] not in raw  # never in the settings file
    secret_file = Path(status["secret_path"])
    assert secret_file.exists()
    assert oct(0o600) == oct(secret_file.stat().st_mode & 0o777)

    with pytest.raises(BackendError):
        backend.link_configure({"server_url": "nope", "client_id": "x"})
    with pytest.raises(BackendError):
        backend.link_configure({"server_url": "http://u:p@s:8000",
                                "client_id": "g560"})


def test_link_operations_require_configuration(tmp_path):
    backend = DesktopBackend(link_config_dir=str(tmp_path / "c"))
    assert backend.link_status() == {"configured": False,
                                     "state": "DISCONNECTED",
                                     "mode": "hybrid"}
    assert backend.link_snapshot() == {"configured": False}  # poll-safe
    with pytest.raises(BackendError):
        backend.link_connect()
    with pytest.raises(BackendError):
        backend.link_disconnect()
    with pytest.raises(BackendError):
        backend.link_set_mode("hybrid")
    with pytest.raises(BackendError):
        backend.link_decide_approval("a-1", True)
    with pytest.raises(BackendError):
        backend.link_mutate_task("t-1", "cancel")
    assert backend.link_decision_preview("implement csv") is None


def test_link_load_restores_saved_configuration(linked):
    backend, env, settings, _root = linked
    backend.link_configure(settings)
    # Simulate a restart: a fresh backend, same config dir.
    fresh = DesktopBackend(link_config_dir=str(settings["config_dir"]))
    status = fresh.link_load()
    assert status["configured"] is True
    assert status["client_id"] == "g560"
    assert status["mode"] == "server"


def test_link_set_mode_persists(linked):
    backend, env, settings, _root = linked
    backend.link_configure(settings)
    status = backend.link_set_mode("hybrid")
    assert status["mode"] == "hybrid"
    reloaded = json.loads(
        (Path(settings["config_dir"]) / "desktop_client.json")
        .read_text(encoding="utf-8"))
    assert reloaded["mode"] == "hybrid"
    with pytest.raises(BackendError):
        backend.link_set_mode("sideways")


# -- submission routing ----------------------------------------------------------------

def test_submit_routes_server_task_through_link(linked):
    backend, env, settings, _root = linked
    backend.link_configure(settings)
    result = backend.submit_task("demo", "Add CSV export functionality")
    assert result["execution"] == "SERVER"
    assert result["task"]["id"].startswith("t-")
    # The origin was recorded server-side as a SERVER execution.
    origin = env.service.store.get_task_origin(result["task"]["id"])
    assert origin is not None


def test_submit_local_light_task_without_server(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    backend = DesktopBackend(link_config_dir=str(tmp_path / "client"))
    backend.start({"demo": str(repo)})
    try:
        backend.link_configure({
            "server_url": "http://forge-server:8000",
            "client_id": "g560",
            "project_id": "demo",
            "mode": "hybrid",
            "secret": "A" * 43 + "b",
            "config_dir": str(tmp_path / "client"),
            "_sender": lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("unreachable")),
        })
        result = backend.submit_task("demo", "show repository status")
        assert result["execution"] == "LOCAL"
        assert result["task"]["status"] == "SUCCEEDED"
        assert result["task"]["provider"] == "local-light"
    finally:
        backend.stop()


def test_submit_without_link_uses_local_plane(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    backend = DesktopBackend(link_config_dir=str(tmp_path / "client"))
    backend.start({"solo": str(repo)})
    try:
        result = backend.submit_task("solo", "Add CSV export functionality",
                                     mode="safe")
        assert result["execution"] == "LOCAL-PLANE"
    finally:
        backend.stop()


def test_stop_does_not_cancel_server_tasks(linked):
    backend, env, settings, _root = linked
    backend.link_configure(settings)
    result = backend.submit_task("demo", "Add CSV export functionality")
    server_task_id = result["task"]["id"]
    backend.stop()  # cancels local-plane work only
    run = env.plane.runs.get(server_task_id)
    assert run.status.value != "CANCELLED"


# -- snapshot / restore -----------------------------------------------------------------

def test_link_snapshot_shape_and_restore(linked):
    backend, env, settings, _root = linked
    backend.link_configure(settings)
    result = backend.submit_task("demo", "Add CSV export functionality")

    snapshot = backend.link_snapshot(full=True)
    assert snapshot["configured"] is True
    assert snapshot["connection"]["state"] in ("CONNECTED",
                                               "RECONNECTING",
                                               "DISCONNECTED")
    ids = [t["id"] for t in snapshot["tasks"]]
    assert result["task"]["id"] in ids

    # Restore-on-reopen (requirement 9) through the backend path.
    restored = backend.link_connect()
    restored_ids = [t["id"] for t in restored.get("tasks", [])]
    assert result["task"]["id"] in restored_ids
    assert restored["connection"]["configured"] is True


def test_link_decision_preview(linked):
    backend, env, settings, _root = linked
    backend.link_configure(settings)
    preview = backend.link_decision_preview("implement CSV export")
    assert preview["execution"] in ("SERVER", "LOCAL")
    fallback = backend.link_decision_preview(fallback=True)
    assert "type a task" in fallback["reason"]


def test_approval_and_mutation_proxies(linked):
    backend, env, settings, _root = linked
    backend.link_configure(settings)
    result = backend.submit_task("demo", "Add CSV export functionality")
    task_id = result["task"]["id"]

    def approved_all():
        # Approve through the backend proxy until the task completes.
        snapshot = backend.link_snapshot(selected_task=task_id, full=True)
        approvals = snapshot.get("approvals", [])
        for approval in approvals:
            backend.link_decide_approval(approval["id"], True)
        for task in snapshot.get("tasks", []):
            if task["id"] == task_id and task["status"] in ("SUCCEEDED",
                                                            "FAILED"):
                return task
        return None

    final = wait_until(approved_all, timeout=60)
    assert final["status"] == "SUCCEEDED"
    with pytest.raises(BackendError):
        backend.link_mutate_task(task_id, "explode")


# -- GUI (stub tkinter) ---------------------------------------------------------------------

def _stub_gui(monkeypatch):
    from test_desktop_app_gui_stub import _install_stub_tkinter
    return _install_stub_tkinter(monkeypatch)


def test_gui_server_tab_renders_snapshot(monkeypatch, linked):
    gui = _stub_gui(monkeypatch)
    backend, env, settings, _root = linked
    backend.link_configure(settings)

    app = gui.ForgeDesktopApp(backend)
    app._current_project.set("demo")
    snapshot = {
        "configured": True,
        "connection": {"configured": True, "state": "CONNECTED",
                       "server_url": "http://forge-server:8000",
                       "mode": "server", "detail": ""},
        "server_info": {"workers": 4, "models": ["m/a81"],
                        "model_ready": True, "server": "forge-server"},
        "tasks": [{"id": "t-1", "status": "RUNNING", "stage": "coding",
                   "execution": "SERVER", "model": "m/a81",
                   "provider": "scripted", "actor": "link-g560",
                   "requirement": "add csv"}],
        "queue": {"depth": 2},
        "approvals": [{"id": "a-1", "label": "write app.py"}],
        "verification": {"tests": {"passed": 1}, "acceptance": {
            "accepted": True}},
        "selected_task": "t-1",
        "logs": ["[00:00:00] INFO: hello", "[00:00:01] ROUTE: SERVER — x"],
        "decision_preview": {"execution": "SERVER", "reason": "mode=server"},
    }
    app._apply_link_snapshot(snapshot)
    app._apply_link_snapshot({**snapshot, "approvals": [],
                              "verification": {}})
    app._apply_link_snapshot({"configured": False})
    app._on_close()


def test_gui_server_tab_callbacks_guard_empty_state(monkeypatch, linked):
    gui = _stub_gui(monkeypatch)
    backend, env, settings, _root = linked
    backend.link_configure(settings)
    app = gui.ForgeDesktopApp(backend)
    # No selection: mutations and decisions must not raise.
    app._link_mutate("pause", "Paused")
    app._link_decide(True)
    app._link_read_secret()
    app._focus_server_tab()
    app._render_link_status({"state": "RECONNECTING",
                             "server_url": "http://x:1",
                             "detail": "refused"})
    app._on_close()


def test_gui_connect_button_rejects_incomplete_form(monkeypatch, linked):
    gui = _stub_gui(monkeypatch)
    backend, env, settings, _root = linked
    app = gui.ForgeDesktopApp(backend)
    app._link_url.insert(0, "")
    app._link_connect_clicked()  # must show info, not raise
    app._on_close()
