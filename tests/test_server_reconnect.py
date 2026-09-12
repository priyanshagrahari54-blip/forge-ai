"""Client reconnection tests (A81).

The Forge Server must keep working while no client is connected, and a
reconnecting Forge Desktop must recover everything in one call: active
tasks, progress, logs, results, and pending approval requests — with
event replay that is exact from the client's last cursor.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_server import (  # noqa: E402
    TEST_TOKEN,
    ApprovalExecutor,
    BlockingExecutor,
    auth_headers,
    create_task,
    event_types,
    make_client,
    make_server,
    success_executor,
    wait_for_status,
    wait_for_status_http,
    wait_until,
)

from forge.server import TaskStatus  # noqa: E402

ADMIN = auth_headers(TEST_TOKEN)


def test_server_works_while_client_is_disconnected(tmp_path):
    blocker = BlockingExecutor()
    server = make_server(tmp_path, blocker.as_executor())
    task_id = ""
    client = make_client(server)
    try:
        # Phase 1: a connected client submits work.
        with client:
            task = create_task(client, ADMIN, "offline-friendly work")
            task_id = task["task_id"]
            wait_for_status_http(client, ADMIN, task_id, {"running"})
        # The client is now gone (context manager exited).

        # Phase 2: the server keeps working with nobody connected.
        assert blocker.entered.is_set()
        blocker.release.set()
        done = wait_for_status(server, task_id, {TaskStatus.COMPLETED},
                               timeout=15)
        assert done.status == TaskStatus.COMPLETED
        assert done.result()["blocked"] is True

        # Phase 3: a brand-new client reconnects and sees everything.
        reconnected = make_client(server)
        with reconnected:
            recovery = reconnected.get("/api/v1/recovery",
                                       headers=ADMIN).json()
            finished_ids = [task["task_id"]
                            for task in recovery["finished_tasks"]]
            assert task_id in finished_ids
            finished = [task for task in recovery["finished_tasks"]
                        if task["task_id"] == task_id][0]
            assert finished["status"] == "completed"
            assert finished["result"]["blocked"] is True
            kinds = [item["kind"]
                     for item in recovery["notifications"]]
            assert "task.completed" in kinds
            # Full history replays from cursor 0.
            assert task_id in recovery["events"]
            types = event_types(recovery["events"][task_id])
            assert "task.created" in types
            assert "task.completed" in types
    finally:
        blocker.release.set()
        server.close()


def test_recovery_bundle_carries_active_task_progress(tmp_path):
    blocker = BlockingExecutor()
    server = make_server(tmp_path, blocker.as_executor())
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "long running work")
            task_id = task["task_id"]
            wait_for_status_http(client, ADMIN, task_id, {"running"})

            recovery = client.get("/api/v1/recovery",
                                  headers=ADMIN).json()
            assert recovery["protocol_version"] == 1
            assert recovery["server"]["boot_id"] == server.boot_id
            assert len(recovery["active_tasks"]) == 1
            active = recovery["active_tasks"][0]
            assert active["task_id"] == task_id
            assert active["status"] == "running"
            assert active["stage"] == "coding"  # scripted stage
            assert active["progress"] > 0
            assert active["checkpoint_id"]
            assert active["retry_count"] == 0
            # Logs are part of the bundle.
            assert task_id in recovery["logs"]
            messages = [entry["message"]
                        for entry in recovery["logs"][task_id]]
            assert any("Worker picked up task" in item
                       for item in messages)
            # Project registry is included.
            assert [project["project_id"]
                    for project in recovery["projects"]] == ["demo"]
            assert recovery["task_counts"]["running"] == 1
    finally:
        blocker.release.set()
        server.close()


def test_recovery_replays_events_exactly_from_cursor(tmp_path):
    blocker = BlockingExecutor()
    server = make_server(tmp_path, blocker.as_executor())
    first_client = make_client(server)
    try:
        # Client A watches the beginning, records its cursor, disconnects.
        with first_client:
            task = create_task(first_client, ADMIN, "cursor replay")
            task_id = task["task_id"]
            wait_for_status_http(first_client, ADMIN, task_id, {"running"})
            seen = first_client.get(
                "/api/v1/tasks/%s/events?limit=500" % task_id,
                headers=ADMIN).json()
            cursor = seen["latest_seq"]
            seen_types = event_types(seen["events"])

        # The server keeps emitting events while nobody listens.
        server.emit(task_id, "demo", "offline.progress", {"step": 1})
        server.emit(task_id, "demo", "offline.progress", {"step": 2})

        # Client B reconnects with the cursor: it receives exactly the
        # missed events — no gaps, no duplicates.
        second_client = make_client(server)
        with second_client:
            recovery = second_client.get(
                "/api/v1/recovery?after=%d" % cursor,
                headers=ADMIN).json()
            replayed = recovery["events"][task_id]
            assert event_types(replayed) == ["offline.progress",
                                             "offline.progress"]
            assert [event["data"]["step"] for event in replayed] == [1, 2]
            assert all(event["seq"] > cursor for event in replayed)
            # The returned cursor lets the client continue seamlessly.
            assert recovery["cursors"]["latest_event_seq"] >= \
                replayed[-1]["seq"]
            full = second_client.get(
                "/api/v1/tasks/%s/events?limit=500" % task_id,
                headers=ADMIN).json()
            assert seen_types + ["offline.progress",
                                 "offline.progress"] == \
                event_types(full["events"])[:len(seen_types) + 2]
    finally:
        blocker.release.set()
        server.close()


def test_reconnect_recovers_pending_approval_and_completes(tmp_path):
    approver = ApprovalExecutor()
    server = make_server(tmp_path, approver.as_executor())
    first_client = make_client(server)
    try:
        # Client A submits, sees the approval request, disconnects
        # *without deciding* — the task must keep waiting, durably.
        session_token = ""
        with first_client:
            session = first_client.post("/api/v1/auth/sessions",
                                        headers=ADMIN).json()
            session_token = session["token"]
            task = create_task(first_client, ADMIN, "approval across "
                                                    "reconnect")
            task_id = task["task_id"]
            wait_for_status_http(first_client, ADMIN, task_id,
                                 {"waiting_for_approval"})

        # Nobody is connected now; the approval stays pending.
        time.sleep(0.3)
        assert server.tasks.get(task_id).status == \
            TaskStatus.WAITING_FOR_APPROVAL
        assert len(server.approvals.pending()) == 1

        # Client B reconnects *with its old session token* and recovers.
        second_client = make_client(server)
        with second_client:
            headers = auth_headers(session_token)
            who = second_client.get("/api/v1/auth/whoami",
                                    headers=headers)
            assert who.status_code == 200  # the session survived

            recovery = second_client.get("/api/v1/recovery",
                                         headers=headers).json()
            assert [item["task_id"]
                    for item in recovery["active_tasks"]] == [task_id]
            assert recovery["active_tasks"][0]["status"] == \
                "waiting_for_approval"
            assert len(recovery["approvals"]) == 1
            approval = recovery["approvals"][0]
            assert approval["payload"]["paths"] == ["app.py"]
            kinds = [item["kind"] for item in recovery["notifications"]]
            assert "approval.requested" in kinds

            # The reconnected client decides the approval.
            decided = second_client.post(
                "/api/v1/approvals/%s/decide" % approval["approval_id"],
                headers=headers, json={"approved": True})
            assert decided.status_code == 200

            done = wait_for_status_http(second_client, headers, task_id,
                                        {"completed"}, timeout=15)
            assert done["status"] == "completed"
            assert done["result"]["approved"] is True
            assert approver.token
    finally:
        server.close()


def test_recovery_bundle_is_bounded(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            for index in range(5):
                task = create_task(client, ADMIN,
                                   "bounded work %d" % index)
                wait_for_status_http(client, ADMIN, task["task_id"],
                                     {"completed"})
            recovery = client.get("/api/v1/recovery",
                                  headers=ADMIN).json()
            # Finished tasks carry results, but the list itself is capped.
            assert len(recovery["finished_tasks"]) <= 50
            for entries in recovery["logs"].values():
                assert len(entries) <= 100
            for entries in recovery["events"].values():
                assert len(entries) <= 200
            # The since-filter narrows results to a time window.
            future = client.get(
                "/api/v1/recovery?since=%f" % (time.time() + 3600),
                headers=ADMIN).json()
            assert future["finished_tasks"] == []
    finally:
        server.close()


def test_logs_page_with_cursor_on_reconnect(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "loggy work")
            wait_for_status_http(client, ADMIN, task["task_id"],
                                 {"completed"})
            page_one = client.get(
                "/api/v1/tasks/%s/logs?limit=2" % task["task_id"],
                headers=ADMIN).json()
            assert len(page_one["logs"]) == 2
            cursor = page_one["logs"][-1]["id"]
            page_two = client.get(
                "/api/v1/tasks/%s/logs?after=%d" % (task["task_id"],
                                                    cursor),
                headers=ADMIN).json()
            assert all(entry["id"] > cursor
                       for entry in page_two["logs"])
            assert page_two["latest_id"] >= cursor
            combined = page_one["logs"] + page_two["logs"]
            ids = [entry["id"] for entry in combined]
            assert ids == sorted(set(ids))  # exact, ordered, no duplicates
    finally:
        server.close()
