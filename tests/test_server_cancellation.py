"""Cancellation, pause/resume, retry, and rollback tests (A81).

Control is cooperative and durable: queued tasks cancel/pause instantly,
running tasks stop at the next control checkpoint (exactly like the
Supervisor's stage boundaries), paused and cancelled states survive in
SQLite, and rollback restores the server's pre-run checkpoint.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_server import (  # noqa: E402
    BlockingExecutor,
    FileWritingExecutor,
    event_types,
    make_server,
    success_executor,
    wait_for_status,
    wait_until,
)

from forge.server import TaskStatus  # noqa: E402
from forge.server.errors import Conflict  # noqa: E402


def test_cancel_queued_task_is_immediate(tmp_path):
    server = make_server(tmp_path, success_executor(), start=False)
    try:
        task = server.submit_task("demo", "Never started")
        assert task.status == TaskStatus.QUEUED
        cancelled = server.cancel_task(task.task_id, actor="tester")
        assert cancelled.status == TaskStatus.CANCELLED
        assert cancelled.finished_at is not None
        assert not server.queue.contains(task.task_id)
        # Starting the server now must not resurrect the cancelled task.
        server.start()
        time.sleep(0.4)
        assert server.tasks.get(task.task_id).status == \
            TaskStatus.CANCELLED
    finally:
        server.close()


def test_cancel_running_task_is_cooperative(tmp_path):
    blocker = BlockingExecutor()
    server = make_server(tmp_path, blocker.as_executor())
    try:
        task = server.submit_task("demo", "Long work")
        running = wait_for_status(server, task.task_id,
                                  {TaskStatus.RUNNING})
        assert running.status == TaskStatus.RUNNING

        cancelling = server.cancel_task(task.task_id, actor="tester")
        # The API returns immediately; cancellation is in flight.
        assert cancelling.cancel_requested
        cancelled = wait_for_status(server, task.task_id,
                                    {TaskStatus.CANCELLED}, timeout=10)
        assert cancelled.status == TaskStatus.CANCELLED
        assert cancelled.finished_at is not None
        # Events/notifications are written just after the status flip;
        # poll instead of reading them in the same instant.
        def _cancellation_recorded() -> bool:
            events, _ = server.events.list(task.task_id, limit=500)
            types = event_types([e.to_dict() for e in events])
            return ("task.cancelling" in types and "task.cancelled" in types
                    and server.notifications.unread_count("demo") >= 1)

        assert wait_until(_cancellation_recorded, timeout=10)
    finally:
        blocker.release.set()
        server.close()


def test_cancel_completed_task_conflicts(tmp_path):
    server = make_server(tmp_path, success_executor())
    try:
        task = server.submit_task("demo", "Quick work")
        wait_for_status(server, task.task_id, {TaskStatus.COMPLETED})
        with pytest.raises(Conflict):
            server.cancel_task(task.task_id)
    finally:
        server.close()


def test_pause_and_resume_queued_task(tmp_path):
    blocker = BlockingExecutor()
    server = make_server(tmp_path, blocker.as_executor(),
                         max_tasks_per_project=1)
    try:
        first = server.submit_task("demo", "occupies the worker")
        second = server.submit_task("demo", "pause me while queued")
        assert blocker.entered.wait(timeout=10)
        wait_for_status(server, second.task_id, {TaskStatus.QUEUED})

        paused = server.pause_task(second.task_id, actor="tester")
        assert paused.status == TaskStatus.PAUSED
        assert not server.queue.contains(second.task_id)

        # While paused it must not run, even after the first finishes.
        blocker.release.set()
        wait_for_status(server, first.task_id, {TaskStatus.COMPLETED})
        time.sleep(0.4)
        assert server.tasks.get(second.task_id).status == \
            TaskStatus.PAUSED

        # Pause is idempotently refused while already paused.
        with pytest.raises(Conflict):
            server.pause_task(second.task_id)

        resumed = server.resume_task(second.task_id, actor="tester")
        assert resumed.status == TaskStatus.QUEUED
        # blocker.release is still set from the first task, so the
        # resumed task runs straight through.
        done = wait_for_status(server, second.task_id,
                               {TaskStatus.COMPLETED}, timeout=15)
        assert done.status == TaskStatus.COMPLETED
    finally:
        blocker.release.set()
        server.close()


def test_pause_running_task_at_boundary_then_resume(tmp_path):
    blocker = BlockingExecutor()
    server = make_server(tmp_path, blocker.as_executor())
    try:
        task = server.submit_task("demo", "pausable work")
        wait_for_status(server, task.task_id, {TaskStatus.RUNNING})

        server.pause_task(task.task_id, actor="tester")
        paused = wait_for_status(server, task.task_id, {TaskStatus.PAUSED},
                                 timeout=10)
        assert paused.status == TaskStatus.PAUSED

        # While paused the executor is blocked at a control checkpoint.
        calls_while_paused = blocker.calls
        time.sleep(0.3)
        assert blocker.calls == calls_while_paused

        server.resume_task(task.task_id, actor="tester")
        resumed = wait_for_status(server, task.task_id,
                                  {TaskStatus.RUNNING}, timeout=10)
        assert resumed.status == TaskStatus.RUNNING

        blocker.release.set()
        done = wait_for_status(server, task.task_id,
                               {TaskStatus.COMPLETED}, timeout=10)
        assert done.status == TaskStatus.COMPLETED
        events, _ = server.events.list(task.task_id, limit=500)
        types = event_types([e.to_dict() for e in events])
        assert "task.paused" in types
        assert "task.resumed" in types
    finally:
        blocker.release.set()
        server.close()


def test_cancel_while_paused(tmp_path):
    blocker = BlockingExecutor()
    server = make_server(tmp_path, blocker.as_executor())
    try:
        task = server.submit_task("demo", "pause then cancel")
        wait_for_status(server, task.task_id, {TaskStatus.RUNNING})
        server.pause_task(task.task_id)
        wait_for_status(server, task.task_id, {TaskStatus.PAUSED})

        server.cancel_task(task.task_id, actor="tester")
        cancelled = wait_for_status(server, task.task_id,
                                    {TaskStatus.CANCELLED}, timeout=10)
        assert cancelled.status == TaskStatus.CANCELLED
    finally:
        blocker.release.set()
        server.close()


def test_resume_non_paused_conflicts(tmp_path):
    server = make_server(tmp_path, success_executor())
    try:
        task = server.submit_task("demo", "quick")
        wait_for_status(server, task.task_id, {TaskStatus.COMPLETED})
        with pytest.raises(Conflict):
            server.resume_task(task.task_id)
    finally:
        server.close()


def test_retry_failed_task(tmp_path):
    calls = {"n": 0}

    def execute(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"accepted": False, "error": "first attempt rejected"}
        return {"accepted": True, "result": {"attempt": calls["n"]}}

    from forge.server.executor import CallableExecutor

    server = make_server(tmp_path, CallableExecutor(execute),
                         max_retries=0)
    try:
        task = server.submit_task("demo", "retry me manually")
        failed = wait_for_status(server, task.task_id, {TaskStatus.FAILED})
        assert failed.status == TaskStatus.FAILED

        retried = server.retry_task(task.task_id, actor="tester")
        assert retried.status == TaskStatus.QUEUED
        done = wait_for_status(server, task.task_id,
                               {TaskStatus.COMPLETED}, timeout=15)
        assert done.status == TaskStatus.COMPLETED
        assert done.result()["attempt"] == 2
    finally:
        server.close()


def test_retry_completed_task_conflicts(tmp_path):
    server = make_server(tmp_path, success_executor())
    try:
        task = server.submit_task("demo", "quick")
        wait_for_status(server, task.task_id, {TaskStatus.COMPLETED})
        with pytest.raises(Conflict):
            server.retry_task(task.task_id)
    finally:
        server.close()


def test_rollback_restores_pre_run_state(tmp_path):
    writer = FileWritingExecutor("generated.txt", "generated\n")
    server = make_server(tmp_path, writer.as_executor())
    root = Path(server.projects.get("demo").root)
    try:
        task = server.submit_task("demo", "write a file")
        done = wait_for_status(server, task.task_id,
                               {TaskStatus.COMPLETED})
        assert done.status == TaskStatus.COMPLETED
        assert (root / "generated.txt").exists()

        rolled = server.rollback_task(task.task_id, actor="tester")
        assert rolled.status == TaskStatus.ROLLED_BACK
        assert not (root / "generated.txt").exists()
        # Pre-existing repository content is untouched.
        assert (root / "app.py").exists()
        events, _ = server.events.list(task.task_id, limit=500)
        assert "task.rolled_back" in event_types(
            [e.to_dict() for e in events])
    finally:
        server.close()


def test_rollback_running_task_conflicts(tmp_path):
    blocker = BlockingExecutor()
    server = make_server(tmp_path, blocker.as_executor())
    try:
        task = server.submit_task("demo", "running")
        wait_for_status(server, task.task_id, {TaskStatus.RUNNING})
        with pytest.raises(Conflict):
            server.rollback_task(task.task_id)
    finally:
        blocker.release.set()
        server.close()


def test_rollback_after_restart_reports_honest_conflict(tmp_path):
    writer = FileWritingExecutor()
    db_path = tmp_path / "server" / "server.db"
    server = make_server(tmp_path, writer.as_executor(), db_path=db_path)
    try:
        task = server.submit_task("demo", "write a file")
        wait_for_status(server, task.task_id, {TaskStatus.COMPLETED})
        task_id = task.task_id
    finally:
        server.close()

    # Checkpoints live in memory per boot: after a restart, rollback
    # says so honestly instead of pretending to restore anything.
    restarted = make_server(tmp_path, success_executor(), db_path=db_path,
                            with_repo=False)
    try:
        with pytest.raises(Conflict) as info:
            restarted.rollback_task(task_id)
        assert "restart" in str(info.value)
    finally:
        restarted.close()


def test_cancel_requested_survives_until_worker_observes(tmp_path):
    blocker = BlockingExecutor()
    server = make_server(tmp_path, blocker.as_executor())
    try:
        task = server.submit_task("demo", "cancel me")
        wait_for_status(server, task.task_id, {TaskStatus.RUNNING})
        server.cancel_task(task.task_id)
        # The durable flag is set immediately, status flips cooperatively.
        assert wait_until(
            lambda: server.tasks.get(task.task_id).cancel_requested,
            timeout=5)
        cancelled = wait_for_status(server, task.task_id,
                                    {TaskStatus.CANCELLED}, timeout=10)
        assert cancelled.status == TaskStatus.CANCELLED
        # Terminal state clears the request flag.
        assert not cancelled.cancel_requested
    finally:
        blocker.release.set()
        server.close()
