"""Forge Server end-to-end tests (A81): the real pipeline over HTTP.

These tests drive the *actual* integration stack through the
authenticated HTTP API only — queue, scheduler, worker, Supervisor,
Model Fabric (scripted provider), agents, policy gate, A33 approvals,
checkpoints, tests, review, security verification, acceptance, and the
exact-file git commit — then assert on durable server state (result,
events, logs, repository content, git history).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import CSV_PAYLOAD, FAILING_PAYLOAD  # noqa: E402
from helpers_a34 import ScriptedProvider, make_fabric  # noqa: E402
from helpers_server import (  # noqa: E402
    TEST_TOKEN,
    approve_until_terminal,
    auth_headers,
    create_task,
    event_types,
    git_log,
    make_client,
    make_server,
    wait_for_pending_approval,
    wait_for_status_http,
)

from forge.server import TaskStatus  # noqa: E402

ADMIN = auth_headers(TEST_TOKEN)


def _make_server_with_fabric(tmp_path, payload: str):
    provider = ScriptedProvider(payload)
    fabric = make_fabric(provider)
    server = make_server(tmp_path, executor=None, fabric=fabric)
    return server, provider


def test_full_supervisor_pipeline_over_http(tmp_path):
    server, provider = _make_server_with_fabric(tmp_path, CSV_PAYLOAD)
    client = make_client(server)
    root = Path(server.projects.get("demo").root)
    try:
        with client:
            task = create_task(client, ADMIN,
                               "Add CSV export functionality")
            task_id = task["task_id"]
            assert task["mode"] == "assisted"

            # Assisted profile: the change set stops for approval.
            waiting = wait_for_status_http(
                client, ADMIN, task_id, {"waiting_for_approval"},
                timeout=120)
            assert waiting["status"] == "waiting_for_approval"
            assert waiting["stage"]

            approvals = client.get("/api/v1/approvals",
                                   headers=ADMIN).json()["approvals"]
            assert approvals, "change-set approval must be filed"
            change_approval = approvals[0]
            assert "app.py" in change_approval["payload"]["paths"]
            decided = client.post(
                "/api/v1/approvals/%s/decide"
                % change_approval["approval_id"],
                headers=ADMIN, json={"approved": True})
            assert decided.status_code == 200

            # The commit gate asks a second time (assisted profile).
            # Poll the durable pending list: status observation alone is
            # racy across successive gates.
            commit_approval = wait_for_pending_approval(
                client, ADMIN,
                lambda item: item["payload"]["operation"] == "git:commit",
                timeout=120)
            assert commit_approval is not None, \
                "commit approval must be filed"
            waiting = wait_for_status_http(
                client, ADMIN, task_id, {"waiting_for_approval"}, timeout=30)
            assert waiting["status"] == "waiting_for_approval"
            decided = client.post(
                "/api/v1/approvals/%s/decide"
                % commit_approval["approval_id"],
                headers=ADMIN, json={"approved": True})
            assert decided.status_code == 200

            done = wait_for_status_http(client, ADMIN, task_id,
                                        {"completed", "failed"},
                                        timeout=180)
            assert done["status"] == "completed", done.get("error")

            # The repository really changed and really committed.
            content = (root / "app.py").read_text(encoding="utf-8")
            assert "export_csv" in content
            assert (root / "tests" / "test_csv.py").exists()
            assert "forge: Add CSV export functionality" in git_log(root)

            # The durable result carries the full verified transaction.
            result = client.get("/api/v1/tasks/%s/result" % task_id,
                                headers=ADMIN).json()
            payload = result["result"]
            assert payload["accepted"] is True
            assert sorted(payload["files"]) == ["app.py",
                                                "tests/test_csv.py"]
            assert payload["model"] == "m/a34"
            assert payload["provider"] == "p"
            assert payload["checkpoint_id"]
            assert payload["report"]["final_status"] == "COMPLETED"
            assert payload["report"]["acceptance"]["accepted"] is True
            # Verification integration: post-run security gate summary.
            assert payload["verification"]["gate"] == "security"
            assert payload["verification"]["passed"] is True
            # Git integration: post-run worktree status recorded.
            assert "git" in payload

            # Events tell the whole story, durably.
            events = client.get(
                "/api/v1/tasks/%s/events?limit=500" % task_id,
                headers=ADMIN).json()["events"]
            types = event_types(events)
            for expected in ("task.created", "task.queued",
                             "task.started", "checkpoint.created",
                             "run.started", "agent.selected",
                             "model.selected", "change.proposed",
                             "permission.checked", "approval.required",
                             "approval.approved", "changes.applied",
                             "tests.executed", "review.completed",
                             "security.completed",
                             "acceptance.completed", "git.commit",
                             "task.completed"):
                assert expected in types, expected

            # Logs survived the whole run.
            logs = client.get("/api/v1/tasks/%s/logs?limit=1000"
                              % task_id, headers=ADMIN).json()
            assert logs["latest_id"] > 0
    finally:
        server.close()


def test_rejected_change_set_fails_and_rolls_back(tmp_path):
    server, provider = _make_server_with_fabric(
        tmp_path, FAILING_PAYLOAD)
    client = make_client(server)
    root = Path(server.projects.get("demo").root)
    before = (root / "app.py").read_text(encoding="utf-8")
    try:
        with client:
            task = create_task(client, ADMIN,
                               "Add broken export functionality")
            task_id = task["task_id"]

            # Approve whatever asks; the tests will still fail honestly.
            deadline = time.time() + 120
            final = None
            while time.time() < deadline:
                for approval in client.get(
                        "/api/v1/approvals",
                        headers=ADMIN).json()["approvals"]:
                    client.post(
                        "/api/v1/approvals/%s/decide"
                        % approval["approval_id"],
                        headers=ADMIN, json={"approved": True})
                state = client.get("/api/v1/tasks/%s" % task_id,
                                   headers=ADMIN).json()["task"]
                if state["status"] in ("completed", "failed",
                                       "cancelled"):
                    final = state
                    break
                time.sleep(0.1)
            assert final is not None, "task never reached a terminal state"
            assert final["status"] == "failed"
            assert final["retry_count"] == 0  # honest rejection: no loop

            # The failed candidate left nothing behind (Supervisor
            # rollback), and nothing was committed.
            after = (root / "app.py").read_text(encoding="utf-8")
            assert after == before
            assert not (root / "tests" / "test_csv.py").exists()
            assert "forge:" not in git_log(root)

            result = client.get("/api/v1/tasks/%s/result" % task_id,
                                headers=ADMIN).json()
            assert result["result"]["rollback"] is True
            assert result["result"]["report"]["final_status"] == "FAILED"
    finally:
        server.close()


def test_autonomous_profile_flows_writes_and_gates_only_the_commit(
        tmp_path):
    # Under the autonomous A33 profile, low-risk writes flow without
    # stopping; the git commit stays sensitive (A32) and asks exactly
    # once — the server never loosens that.
    provider = ScriptedProvider(CSV_PAYLOAD)
    fabric = make_fabric(provider)
    server = make_server(tmp_path, executor=None, fabric=fabric,
                         profile="autonomous")
    client = make_client(server)
    root = Path(server.projects.get("demo").root)
    try:
        with client:
            task = create_task(client, ADMIN, "Add CSV export",
                               mode="autonomous")
            assert task["mode"] == "autonomous"
            done = approve_until_terminal(client, ADMIN, task["task_id"],
                                          timeout=180)
            assert done["status"] == "completed", done.get("error")
            # The change-set approval never appeared; only the commit
            # gate asked (exactly one approval across the run).
            records = client.get(
                "/api/v1/tasks/%s/approvals" % task["task_id"],
                headers=ADMIN).json()["approvals"]
            operations = [record["payload"]["operation"]
                          for record in records]
            assert operations == ["git:commit"]
            assert "export_csv" in (root / "app.py").read_text(
                encoding="utf-8")
            assert "forge: Add CSV export" in git_log(root)
    finally:
        server.close()


def test_two_projects_run_independently(tmp_path):
    from helpers_a34 import make_repo

    provider = ScriptedProvider(CSV_PAYLOAD)
    fabric = make_fabric(provider)
    second_root = tmp_path / "second"
    second_root.mkdir()
    make_repo(second_root)
    server = make_server(
        tmp_path, executor=None, fabric=fabric, profile="autonomous",
        extra_projects={"second": str(second_root)}, max_workers=4)
    client = make_client(server)
    try:
        with client:
            first = create_task(client, ADMIN, "Add CSV export one",
                                project_id="demo")
            second = create_task(client, ADMIN, "Add CSV export two",
                                project_id="second")
            for task in (first, second):
                done = approve_until_terminal(
                    client, ADMIN, task["task_id"], timeout=240)
                assert done["status"] == "completed", done.get("error")
            assert "export_csv" in (
                Path(server.projects.get("demo").root) / "app.py"
            ).read_text(encoding="utf-8")
            assert "export_csv" in (second_root / "app.py").read_text(
                encoding="utf-8")
            counts = client.get("/api/v1/status",
                                headers=ADMIN).json()["tasks"]
            assert counts["completed"] == 2
    finally:
        server.close()
