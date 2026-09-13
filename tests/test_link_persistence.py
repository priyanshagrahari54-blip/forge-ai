"""A81 persistence tests: the server continues; reopen restores state.

Requirement 8: if the laptop disconnects while a server task runs, the
server keeps going (its worker pool is independent of any client).
Requirement 9: reopening Forge Desktop restores the server task state.

The BlockingProvider freezes the pipeline mid-flight so the test — not
timing — decides when work continues.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from helpers_link import (
    BlockingProvider,
    LinkEnv,
    make_client_for,
    wait_until,
)


def _bare_repo(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("def health(): return True\n")
    (root / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_app.py").write_text(
        "from app import health\ndef test_health():\n    assert health()\n")
    subprocess.run(["git", "init"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "--", ".gitignore", "app.py",
                    "tests/test_app.py"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root,
                   check=True, capture_output=True)


def test_server_continues_after_client_disconnect_and_reopen_restores(
        tmp_path):
    provider = BlockingProvider()
    env = LinkEnv(tmp_path, provider=provider, approval_timeout=120.0)
    _bare_repo(env.plane.projects["demo"].__dict__.get("root")
               or __import__("pathlib").Path(
                   env.plane.projects["demo"].root))
    try:
        secret = env.register()
        client = make_client_for(env, secret)
        assert client.connect(with_heartbeat=False)

        # 1. Submit a server task; the pipeline enters the model call
        #    and blocks there (deterministic hold point).
        result = client.submit("Add CSV export functionality")
        task_id = result["task"]["id"]
        wait_until(lambda: provider.entered.is_set(), timeout=30)
        assert provider.release.is_set() is False

        # 2. The laptop disappears: the client drops its session and
        #    stops talking. Nothing is cancelled server-side.
        client.disconnect()
        assert client.connection.state_name == "DISCONNECTED"
        run = env.plane.runs.get(task_id)
        assert run.status.value in ("RUNNING", "WAITING_APPROVAL")

        # 3. The server keeps working the task with zero clients.
        provider.release.set()
        deadline = time.time() + 60
        status = ""
        stage = ""
        while time.time() < deadline:
            run = env.plane.runs.get(task_id)
            status, stage = run.status.value, run.stage
            if status in ("SUCCEEDED", "FAILED", "WAITING_APPROVAL"):
                break
            time.sleep(0.2)
        # The pipeline advanced past the blocked model call purely
        # server-side (it now waits for a write approval no one gives).
        assert stage != "coding" or status in ("SUCCEEDED", "FAILED",
                                               "WAITING_APPROVAL")

        # 4. Reopening Forge Desktop: a brand-new client (fresh session)
        #    with the same secret restores the full task state.
        reopened = make_client_for(env, secret)
        snapshot = reopened.restore()
        restored = [t for t in snapshot["tasks"] if t["id"] == task_id]
        assert restored, "reopen must restore the server task"
        assert restored[0]["status"] == status
        assert restored[0]["stage"] == stage
        assert restored[0]["execution"] == "SERVER"
        assert restored[0]["mode"] == "assisted"
        assert snapshot["event_cursor"] > 0
        assert snapshot["server_info"]["model_ready"] is True

        # 5. The reopened client resumes control: approving the pending
        #    write completes the task end to end.
        def finished():
            snap = reopened.snapshot(selected_task=task_id, full=True)
            for approval in snap["approvals"]:
                reopened.decide_approval(approval["id"], True)
            for task in snap["tasks"]:
                if task["id"] == task_id and task["status"] in (
                        "SUCCEEDED", "FAILED"):
                    return task
            return None

        final = wait_until(finished, timeout=60)
        assert final["status"] == "SUCCEEDED"
        assert final["files"] == ["app.py", "tests/test_csv.py"]
    finally:
        provider.release.set()


def test_disconnect_leaves_no_cancel_path(tmp_path):
    """Closing the client never cancels server work (requirement 8)."""
    provider = BlockingProvider()
    env = LinkEnv(tmp_path, provider=provider, approval_timeout=120.0)
    _bare_repo(Path(env.plane.projects["demo"].root))
    try:
        secret = env.register()
        client = make_client_for(env, secret)
        assert client.connect(with_heartbeat=False)
        task_id = client.submit("Add CSV export functionality")["task"]["id"]
        wait_until(lambda: provider.entered.is_set(), timeout=30)

        client.disconnect()
        # The run is still live and uncancelled.
        run = env.plane.runs.get(task_id)
        assert run.status.value != "CANCELLED"

        # ... and a reopen sees it too.
        reopened = make_client_for(env, secret)
        snapshot = reopened.restore()
        match = [t for t in snapshot["tasks"] if t["id"] == task_id]
        assert match and match[0]["status"] != "CANCELLED"
    finally:
        provider.release.set()
