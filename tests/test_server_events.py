"""Persistent event system tests (A81).

Events are the live-update channel: durable in SQLite, monotonic per
task, exactly replayable from any cursor (the reconnection contract),
long-pollable, size-capped, and secret-redacted before storage.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_server import (  # noqa: E402
    TEST_TOKEN,
    auth_headers,
    event_types,
    make_client,
    make_server,
    success_executor,
    wait_for_status,
)

from forge.server import TaskStatus  # noqa: E402
from forge.server.events import MAX_EVENT_BYTES, EventStore  # noqa: E402
from forge.server.storage import Database  # noqa: E402


# -- store-level unit tests ----------------------------------------------------

def test_events_persist_across_reopen(tmp_path):
    db_path = tmp_path / "events.db"
    store = EventStore(Database(db_path))
    first = store.append("t1", "demo", "task.created", {"n": 1})
    second = store.append("t1", "demo", "task.queued", {"n": 2})

    reopened = EventStore(Database(db_path))
    events, latest = reopened.list("t1")
    assert [event.type for event in events] == ["task.created",
                                                "task.queued"]
    assert latest == second.seq
    assert first.seq < second.seq


def test_cursor_replay_is_exact(tmp_path):
    store = EventStore(Database(tmp_path / "events.db"))
    seqs = [store.append("t1", "demo", "e%d" % index).seq
            for index in range(10)]

    # Replay from the middle: exactly the missed events, in order.
    events, latest = store.list("t1", after=seqs[4])
    assert [event.seq for event in events] == seqs[5:]
    assert latest == seqs[-1]

    # Replay from the end: nothing new, no duplicates.
    events, _ = store.list("t1", after=seqs[-1])
    assert events == []


def test_sequences_are_monotonic_and_never_reused(tmp_path):
    store = EventStore(Database(tmp_path / "events.db"))
    previous = 0
    for index in range(25):
        event = store.append("t1", "demo", "tick", {"index": index})
        assert event.seq > previous
        previous = event.seq


def test_per_task_buffer_is_bounded(tmp_path):
    store = EventStore(Database(tmp_path / "events.db"),
                       max_events_per_task=100)
    for index in range(150):
        store.append("t1", "demo", "tick", {"index": index})
    events, latest = store.list("t1", limit=500)
    assert len(events) <= 100
    # Pruning keeps the newest events and monotonic sequences.
    assert latest >= 150
    assert events[-1].data["index"] == 149


def test_oversized_payloads_are_truncated_not_stored(tmp_path):
    store = EventStore(Database(tmp_path / "events.db"))
    huge = store.append("t1", "demo", "big",
                        {"blob": "x" * (MAX_EVENT_BYTES * 2)})
    assert huge.data.get("truncated") is True
    assert len(str(huge.data.get("preview", ""))) <= MAX_EVENT_BYTES


def test_secret_looking_payloads_are_redacted(tmp_path):
    store = EventStore(Database(tmp_path / "events.db"))
    event = store.append("t1", "demo", "leak", {
        "aws": "AKIA" + "A" * 16,
        "bearer": "Bearer " + "a" * 40,
        "safe": "nothing to see"})
    assert "AKIA" + "A" * 16 not in str(event.data)
    assert event.data["safe"] == "nothing to see"


def test_wait_returns_when_new_event_arrives(tmp_path):
    store = EventStore(Database(tmp_path / "events.db"))
    store.append("t1", "demo", "first")
    latest = store.latest_seq("t1")

    def later():
        time.sleep(0.2)
        store.append("t1", "demo", "second")

    thread = threading.Thread(target=later, daemon=True)
    thread.start()
    events = store.wait("t1", latest, timeout=5.0)
    thread.join(timeout=5)
    assert [event.type for event in events] == ["second"]


def test_wait_times_out_quietly(tmp_path):
    store = EventStore(Database(tmp_path / "events.db"))
    started = time.time()
    assert store.wait("t1", 0, timeout=0.3) == []
    assert time.time() - started < 3.0


# -- server-level integration ------------------------------------------------------

def test_lifecycle_events_are_recorded_in_order(tmp_path):
    server = make_server(tmp_path, success_executor())
    try:
        task = server.submit_task("demo", "Eventful work", actor="tester")
        wait_for_status(server, task.task_id, {TaskStatus.COMPLETED})
        events, _ = server.events.list(task.task_id, limit=500)
        types = event_types([event.to_dict() for event in events])
        for expected in ("task.created", "task.queued", "task.started",
                         "checkpoint.created", "task.running",
                         "task.completed"):
            assert expected in types
        assert types.index("task.created") < types.index("task.queued")
        assert types.index("task.queued") < types.index("task.started")
        assert types.index("task.started") < types.index("task.running")
        assert types.index("task.running") < types.index("task.completed")
    finally:
        server.close()


def test_events_endpoint_replays_from_cursor(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.post(
                "/api/v1/tasks", headers=auth_headers(),
                json={"project_id": "demo", "requirement": "Eventful"})
            task_id = response.json()["task"]["task_id"]
            wait_for_status(server, task_id, {TaskStatus.COMPLETED})

            first = client.get(
                "/api/v1/tasks/%s/events?limit=2" % task_id,
                headers=auth_headers()).json()
            assert len(first["events"]) == 2
            cursor = first["events"][-1]["seq"]

            second = client.get(
                "/api/v1/tasks/%s/events?after=%d" % (task_id, cursor),
                headers=auth_headers()).json()
            assert all(event["seq"] > cursor
                       for event in second["events"])
            # Historical + replayed reconcile without gaps or duplicates.
            all_events = client.get(
                "/api/v1/tasks/%s/events?limit=500" % task_id,
                headers=auth_headers()).json()["events"]
            replayed = first["events"] + second["events"] + \
                client.get(
                    "/api/v1/tasks/%s/events?after=%d&limit=500"
                    % (task_id, second["latest_seq"]),
                    headers=auth_headers()).json()["events"]
            assert [event["seq"] for event in replayed] == \
                [event["seq"] for event in all_events]
    finally:
        server.close()


def test_events_long_poll_waits_for_live_updates(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            response = client.post(
                "/api/v1/tasks", headers=auth_headers(),
                json={"project_id": "demo", "requirement": "Live"})
            task_id = response.json()["task"]["task_id"]
            current = client.get(
                "/api/v1/tasks/%s/events" % task_id,
                headers=auth_headers()).json()["latest_seq"]

            # Long poll with a short wait: the running task emits events,
            # so the call returns before the wait ceiling with them.
            started = time.time()
            payload = client.get(
                "/api/v1/tasks/%s/events?after=%d&wait=10"
                % (task_id, current),
                headers=auth_headers()).json()
            elapsed = time.time() - started
            assert payload["events"], "long poll returned no events"
            assert elapsed < 10
    finally:
        server.close()


def test_events_require_authentication(tmp_path):
    server = make_server(tmp_path, success_executor())
    client = make_client(server)
    try:
        with client:
            task = server.submit_task("demo", "guarded")
            response = client.get(
                "/api/v1/tasks/%s/events" % task.task_id)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    finally:
        server.close()
