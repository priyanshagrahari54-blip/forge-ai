"""Restart-recovery tests (A81).

A crashed or restarted Forge Server must come back honest:

* queued work is still queued and gets dispatched by the new boot;
* work a dead process left mid-flight is re-queued within its retry
  budget (or failed with a clear error once exhausted);
* stale leases are cleared, and a zombie worker from the old boot is
  fenced off — it discards its outcome instead of clobbering fresh state;
* sessions, events, logs, results, and notifications all survive;
* pending approvals expire at the boot boundary (they cannot mint
  tokens whose A33 request died with the old process).
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
    event_types,
    make_server,
    success_executor,
    wait_for_status,
    wait_until,
)

from forge.server import ForgeServer, ServerConfig, TaskStatus  # noqa: E402
from forge.server.executor import CallableExecutor  # noqa: E402


def _config(tmp_path, db_path, executor, **kwargs):
    root = tmp_path / "demo"
    return ServerConfig(
        db_path=str(db_path),
        projects={"demo": str(root)},
        bootstrap_token=TEST_TOKEN,
        executor=executor,
        default_max_retries=kwargs.pop("max_retries", 2),
        retry_backoff_seconds=0.05,
        poll_interval=0.05,
        approval_timeout=kwargs.pop("approval_timeout", 60.0),
        **kwargs)


def test_queued_tasks_survive_restart(tmp_path):
    from helpers_server import make_repo

    root = tmp_path / "demo"
    root.mkdir(parents=True)
    make_repo(root)
    db_path = tmp_path / "server" / "server.db"

    # Boot 1: submit work but never start the workers — a "crash" before
    # dispatch. (start=False keeps everything purely durable.)
    first = ForgeServer(_config(tmp_path, db_path, success_executor("b1")))
    task = first.submit_task("demo", "survive the restart")
    first.close()
    assert task.status == TaskStatus.QUEUED

    # Boot 2: the queued task is picked up and completed.
    second = ForgeServer(_config(tmp_path, db_path,
                                 success_executor("b2")))
    second.start()
    try:
        done = wait_for_status(second, task.task_id,
                               {TaskStatus.COMPLETED}, timeout=15)
        assert done.status == TaskStatus.COMPLETED
        assert done.result()["summary"] == "b2"
    finally:
        second.close()


def test_interrupted_task_is_requeued_with_retry_budget(tmp_path):
    from helpers_server import make_repo

    root = tmp_path / "demo"
    root.mkdir(parents=True)
    make_repo(root)
    db_path = tmp_path / "server" / "server.db"

    blocker = BlockingExecutor()
    first = ForgeServer(_config(tmp_path, db_path, blocker.as_executor(),
                                max_retries=2))
    first.start()
    task = first.submit_task("demo", "interrupted by crash")
    running = wait_for_status(first, task.task_id, {TaskStatus.RUNNING})
    assert running.status == TaskStatus.RUNNING
    task_id = task.task_id

    # Boot 2 starts *while* the old worker is still blocked (crash overlap).
    second = ForgeServer(_config(tmp_path, db_path,
                                 success_executor("b2"), max_retries=2))
    second.start()
    try:
        # Restart recovery re-queued the interrupted task (retry 1)...
        recovered = wait_for_status(second, task_id,
                                    {TaskStatus.RUNNING,
                                     TaskStatus.COMPLETED}, timeout=15)
        assert recovered is not None
        done = wait_for_status(second, task_id, {TaskStatus.COMPLETED},
                               timeout=15)
        assert done.status == TaskStatus.COMPLETED
        assert done.retry_count == 1
        assert done.result()["summary"] == "b2"

        events, _ = second.events.list(task_id, limit=500)
        types = event_types([event.to_dict() for event in events])
        assert "task.requeued" in types

        # ...and the zombie worker from boot 1 is fenced off: when it
        # finally returns, its outcome is discarded, not applied.
        blocker.release.set()
        assert wait_until(lambda: blocker.calls >= 1 and not
                          second.queue.contains(task_id), timeout=10)
        time.sleep(0.5)  # give the old worker time to (not) write
        final = second.tasks.get(task_id)
        assert final.status == TaskStatus.COMPLETED
        assert final.result()["summary"] == "b2"  # zombie never landed
        logs, _ = second.logs.list(task_id)
        assert any("Lease lost" in entry.message for entry in logs)
    finally:
        blocker.release.set()
        first.close()
        second.close()


def test_interrupted_task_fails_when_retry_budget_exhausted(tmp_path):
    from helpers_server import make_repo

    root = tmp_path / "demo"
    root.mkdir(parents=True)
    make_repo(root)
    db_path = tmp_path / "server" / "server.db"

    blocker = BlockingExecutor()
    first = ForgeServer(_config(tmp_path, db_path, blocker.as_executor(),
                                max_retries=0))
    first.start()
    task = first.submit_task("demo", "doomed by crash")
    wait_for_status(first, task.task_id, {TaskStatus.RUNNING})

    second = ForgeServer(_config(tmp_path, db_path,
                                 success_executor("b2"), max_retries=0))
    second.start()
    try:
        failed = wait_for_status(second, task.task_id, {TaskStatus.FAILED},
                                 timeout=15)
        assert failed.status == TaskStatus.FAILED
        assert "restart" in failed.error
        assert "retry budget exhausted" in failed.error
        # The failure was announced durably.
        kinds = [item["kind"]
                 for item in second.notifications.list("demo")]
        assert "task.failed" in kinds
    finally:
        blocker.release.set()
        first.close()
        second.close()


def test_queue_leases_recovered_on_restart(tmp_path):
    from helpers_server import make_repo

    root = tmp_path / "demo"
    root.mkdir(parents=True)
    make_repo(root)
    db_path = tmp_path / "server" / "server.db"

    blocker = BlockingExecutor()
    first = ForgeServer(_config(tmp_path, db_path, blocker.as_executor()))
    first.start()
    task = first.submit_task("demo", "leased work")
    wait_for_status(first, task.task_id, {TaskStatus.RUNNING})
    assert first.queue.leased_count() == 1

    # New boot: stale lease cleared before anything else.
    second = ForgeServer(_config(tmp_path, db_path,
                                 success_executor("b2")))
    try:
        # At construction nothing has run yet; start() does recovery.
        second.start()
        assert wait_until(lambda: second.queue.contains(task.task_id)
                          is False, timeout=15)
        wait_for_status(second, task.task_id, {TaskStatus.COMPLETED},
                        timeout=15)
    finally:
        blocker.release.set()
        first.close()
        second.close()


def test_sessions_events_results_survive_restart(tmp_path):
    from helpers_server import make_repo

    root = tmp_path / "demo"
    root.mkdir(parents=True)
    make_repo(root)
    db_path = tmp_path / "server" / "server.db"

    first = ForgeServer(_config(tmp_path, db_path,
                                success_executor("b1")))
    first.start()
    session, token = first.sessions.create(
        "desktop", "operator", ["tasks:read", "tasks:write"])
    task = first.submit_task("demo", "persist everything")
    wait_for_status(first, task.task_id, {TaskStatus.COMPLETED})
    task_id = task.task_id
    event_count_before = first.events.latest_seq(task_id)
    first.close()

    second = ForgeServer(_config(tmp_path, db_path,
                                 success_executor("b2")))
    second.start()
    try:
        # Session token still authenticates.
        principal = second.authenticate(token)
        assert principal.name == "desktop"
        assert principal.via == "session"

        # Result, events, logs, and notifications are all still there.
        loaded = second.tasks.get_or_raise(task_id)
        assert loaded.status == TaskStatus.COMPLETED
        assert loaded.result()["summary"] == "b1"
        events, latest = second.events.list(task_id, limit=500)
        assert latest == event_count_before
        assert "task.completed" in event_types(
            [event.to_dict() for event in events])
        logs, log_latest = second.logs.list(task_id)
        assert log_latest > 0
        assert second.notifications.unread_count("demo") >= 1

        # A fresh boot id distinguishes the two processes.
        assert second.boot_id != first.boot_id
    finally:
        second.close()


def test_pending_approvals_expire_at_boot_boundary(tmp_path):
    from helpers_server import make_repo

    root = tmp_path / "demo"
    root.mkdir(parents=True)
    make_repo(root)
    db_path = tmp_path / "server" / "server.db"

    approver = ApprovalExecutor()
    first = ForgeServer(_config(tmp_path, db_path, approver.as_executor(),
                                max_retries=1))
    first.start()
    task = first.submit_task("demo", "approval interrupted by restart")
    wait_for_status(first, task.task_id,
                    {TaskStatus.WAITING_FOR_APPROVAL})
    assert len(first.approvals.pending()) == 1
    task_id = task.task_id

    second = ForgeServer(_config(tmp_path, db_path,
                                 success_executor("b2"), max_retries=1))
    second.start()
    try:
        # The stale approval is expired, not silently kept alive...
        assert second.approvals.pending() == []
        expired = [record for record in second.approvals.list_for_task(
            task_id) if record["status"] == "expired"]
        assert expired
        # ...and the interrupted task re-queues and completes on boot 2.
        done = wait_for_status(second, task_id, {TaskStatus.COMPLETED},
                               timeout=15)
        assert done.status == TaskStatus.COMPLETED
        assert done.retry_count == 1
    finally:
        # Release boot 1's worker from its approval wait (its outcome is
        # fenced off anyway) so close() does not wait on the timeout.
        control = first.control_for(task_id)
        if control is not None:
            control.request_cancel()
        first.close()
        second.close()


def test_stop_without_wait_leaves_recoverable_state(tmp_path):
    from helpers_server import make_repo

    root = tmp_path / "demo"
    root.mkdir(parents=True)
    make_repo(root)
    db_path = tmp_path / "server" / "server.db"

    blocker = BlockingExecutor()
    first = ForgeServer(_config(tmp_path, db_path, blocker.as_executor(),
                                max_retries=1))
    first.start()
    task = first.submit_task("demo", "abandoned mid-run")
    wait_for_status(first, task.task_id, {TaskStatus.RUNNING})
    # Hard stop: don't wait for the blocked worker (process-death analog).
    first.stop(wait=False)
    assert not first.running

    second = ForgeServer(_config(tmp_path, db_path,
                                 success_executor("b2"), max_retries=1))
    second.start()
    try:
        done = wait_for_status(second, task.task_id,
                               {TaskStatus.COMPLETED}, timeout=15)
        assert done.status == TaskStatus.COMPLETED
        assert done.retry_count == 1
    finally:
        blocker.release.set()
        first.close()
        second.close()
