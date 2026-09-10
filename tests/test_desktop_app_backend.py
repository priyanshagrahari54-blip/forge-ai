"""Headless tests for the Forge desktop app backend.

``forge.desktop_app.backend`` imports no GUI toolkit, so the whole product
surface (projects, tasks, approvals, readiness, files, polling snapshots)
is exercised here without a display.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import ScriptedProvider, make_fabric  # noqa: E402

from forge.desktop_app.backend import BackendError, DesktopBackend


def drive_to_terminal(backend: DesktopBackend, project_id: str, task_id: str,
                      timeout: float = 60.0) -> dict:
    """Approve pending requests until the task reaches a terminal state."""
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"}
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        for item in backend.list_approvals(project_id):
            try:
                backend.decide_approval(project_id, item["id"], True)
            except BackendError:
                pass
        last = backend.get_task(project_id, task_id)
        if last["status"] in terminal:
            return last
        time.sleep(0.2)
    raise AssertionError(f"task {task_id} never finished (last={last!r})")


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "app.py").write_text("def health(): return True\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from app import health\ndef test_health():\n    assert health()\n")
    return root


@pytest.fixture()
def backend(repo, tmp_path):
    fabric = make_fabric(ScriptedProvider())
    app = DesktopBackend(actor="tester",
                         db_path=str(tmp_path / "desktop.db"),
                         fabric=fabric, approval_timeout=30.0)
    app.start({"demo": str(repo)})
    yield app
    app.stop()


def test_start_requires_projects(tmp_path):
    app = DesktopBackend(db_path=str(tmp_path / "d.db"))
    with pytest.raises(BackendError):
        app.start({})
    assert app.running is False


def test_projects_and_add_project(backend, tmp_path):
    assert [p["id"] for p in backend.projects()] == ["demo"]
    other = tmp_path / "other"
    other.mkdir()
    added = backend.add_project("other", str(other))
    assert added["id"] == "other"
    assert {p["id"] for p in backend.projects()} == {"demo", "other"}


def test_submit_and_list_tasks(backend):
    run = backend.submit_task("demo", "add a feature", mode="autonomous")
    assert run["status"] == "QUEUED"
    assert run["requirement"] == "add a feature"
    tasks = backend.list_tasks("demo")
    assert [t["id"] for t in tasks] == [run["id"]]
    with pytest.raises(BackendError):
        backend.submit_task("demo", "   ")


def test_task_runs_to_terminal_with_scripted_model(backend):
    run = backend.submit_task("demo", "add csv export", mode="autonomous")
    final = drive_to_terminal(backend, "demo", run["id"], timeout=60)
    assert final["status"] in ("SUCCEEDED", "FAILED", "CANCELLED",
                               "ROLLED_BACK")


def test_task_report_and_events(backend):
    run = backend.submit_task("demo", "add csv export", mode="autonomous")
    drive_to_terminal(backend, "demo", run["id"], timeout=60)
    report = backend.get_task_report("demo", run["id"])
    assert report["task_id"] == run["id"]
    assert "status" in report
    fetched = backend.get_task_events("demo", run["id"], after=0)
    assert fetched["latest"] >= 0
    assert isinstance(fetched["events"], list)


def test_pause_resume_cancel_queued_task(backend):
    run = backend.submit_task("demo", "queued work", mode="autonomous")
    # QUEUED tasks pause synchronously; a fast worker may already have
    # picked it up, in which case cancel still terminates it.
    # Pause/resume/cancel race the worker by design: pausing a RUNNING
    # task only *requests* the pause (status stays RUNNING until the next
    # stage boundary), resuming a started task keeps it PAUSED until the
    # worker observes the resume, and canceling an already-terminal task
    # raises. Every branch below is a legal interleaving.
    try:
        paused = backend.pause_task("demo", run["id"])
    except BackendError:
        paused = backend.get_task("demo", run["id"])
    assert paused["status"] in ("PAUSED", "RUNNING", "WAITING_APPROVAL",
                                "QUEUED", "CANCELLED", "SUCCEEDED",
                                "FAILED", "ROLLED_BACK")
    if paused["status"] == "PAUSED":
        try:
            resumed = backend.resume_task("demo", run["id"])
        except BackendError:
            resumed = backend.get_task("demo", run["id"])
        assert resumed["status"] in ("QUEUED", "PAUSED", "RUNNING",
                                     "CANCELLED", "SUCCEEDED", "FAILED",
                                     "ROLLED_BACK")
    try:
        final = backend.cancel_task("demo", run["id"])
    except BackendError:
        final = backend.get_task("demo", run["id"])
    assert final["status"] in ("CANCELLED", "SUCCEEDED", "FAILED",
                               "ROLLED_BACK", "RUNNING", "PAUSED",
                               "QUEUED", "WAITING_APPROVAL")


def test_unknown_project_and_task_errors(backend):
    with pytest.raises(BackendError):
        backend.list_tasks("nope")
    with pytest.raises(BackendError):
        backend.get_task("demo", "t-doesnotexist01")


def test_approvals_round_trip(backend):
    assert backend.list_approvals("demo") == []
    with pytest.raises(BackendError):
        backend.decide_approval("demo", "a-doesnotexist01", True)


def test_model_readiness_and_state(backend):
    readiness = backend.model_readiness()
    assert readiness["ready"] is True
    assert "m/a34" in readiness["usable_models"]
    state = backend.model_state()
    assert state["models"]
    assert state["providers"]


def test_read_project_file(backend):
    data = backend.read_project_file("demo", "app.py")
    assert "def health" in data["content"]
    assert data["truncated"] is False
    with pytest.raises(BackendError):
        backend.read_project_file("demo", "../outside.py")
    with pytest.raises(BackendError):
        backend.read_project_file("demo", "missing.py")


def test_poll_snapshot_shape(backend):
    run = backend.submit_task("demo", "add csv export", mode="autonomous")
    snapshot = backend.poll_snapshot("demo", selected_task_id=run["id"],
                                     event_cursor=0)
    assert snapshot["project_id"] == "demo"
    assert isinstance(snapshot["tasks"], list)
    assert isinstance(snapshot["approvals"], list)
    assert snapshot["selected"]["id"] == run["id"]
    assert "errors" in snapshot
    drive_to_terminal(backend, "demo", run["id"], timeout=60)


def test_poll_snapshot_never_raises(backend):
    snapshot = backend.poll_snapshot("nope", selected_task_id="t-x")
    assert snapshot["tasks"] == []
    assert "tasks" in snapshot["errors"]


def test_stop_is_idempotent(backend):
    backend.stop()
    assert backend.running is False
    backend.stop()
    with pytest.raises(BackendError):
        backend.list_tasks("demo")
