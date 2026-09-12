"""Worker + scheduler integration tests (A81).

Worker failure, bounded retries with backoff, honest non-retry of
pipeline rejections, per-project concurrency caps, and the guarantee
that one bad task never takes the background machinery down: the server
keeps working after any executor failure.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_server import (  # noqa: E402
    BlockingExecutor,
    event_types,
    failing_executor,
    make_server,
    rejected_executor,
    success_executor,
    wait_for_status,
    wait_until,
)

from forge.server import TaskStatus  # noqa: E402
from forge.server.executor import CallableExecutor  # noqa: E402


def test_successful_run_completes_with_result_and_notification(tmp_path):
    server = make_server(tmp_path, success_executor("m1"))
    try:
        task = server.submit_task("demo", "Build it", actor="tester")
        finished = wait_for_status(server, task.task_id,
                                   {TaskStatus.COMPLETED})
        assert finished.status == TaskStatus.COMPLETED
        assert finished.result()["summary"] == "m1"
        assert finished.progress == 1.0
        assert finished.stage == "completed"
        assert finished.error == ""
        assert server.notifications.unread_count("demo") == 1
        kinds = [n["kind"] for n in server.notifications.list("demo")]
        assert "task.completed" in kinds
        # Memory integration: bounded outcome summary in project memory.
        memory_file = (Path(server.projects.get("demo").root)
                       / ".forge" / "memory" / "server" / "tasks"
                       / ("%s.json" % task.task_id))
        assert wait_until(lambda: memory_file.exists(), timeout=10)
        import json as _json

        summary = _json.loads(memory_file.read_text(encoding="utf-8"))
        assert summary["status"] == "completed"
        assert summary["task_id"] == task.task_id
    finally:
        server.close()


def test_worker_failure_retries_then_fails(tmp_path):
    server = make_server(tmp_path, failing_executor("kaput"),
                         max_retries=1)
    try:
        task = server.submit_task("demo", "Flaky work")
        failed = wait_for_status(server, task.task_id, {TaskStatus.FAILED},
                                 timeout=30.0)
        assert failed.status == TaskStatus.FAILED
        assert "kaput" in failed.error
        assert failed.retry_count == 1  # one automatic retry, then failed
        events, _ = server.events.list(task.task_id, limit=500)
        types = event_types([e.to_dict() for e in events])
        assert "task.retrying" in types
        assert "task.failed" in types
        # The retry was visible in the queue (re-enqueued with backoff).
        assert types.count("task.started") == 2
    finally:
        server.close()


def test_worker_recovers_after_transient_failure(tmp_path):
    executor = failing_executor("transient", times=1)
    server = make_server(tmp_path, executor, max_retries=2)
    try:
        task = server.submit_task("demo", "Flaky once")
        done = wait_for_status(server, task.task_id,
                               {TaskStatus.COMPLETED}, timeout=30.0)
        assert done.status == TaskStatus.COMPLETED
        assert done.retry_count == 1
        assert done.result()["recovered_after"] == 2
    finally:
        server.close()


def test_pipeline_rejection_fails_without_retry_loop(tmp_path):
    server = make_server(tmp_path, rejected_executor("tests failed"),
                         max_retries=3)
    try:
        task = server.submit_task("demo", "Bad change")
        failed = wait_for_status(server, task.task_id, {TaskStatus.FAILED})
        assert failed.status == TaskStatus.FAILED
        assert failed.error == "tests failed"
        # Deterministic rejection: no pointless retries.
        assert failed.retry_count == 0
        assert failed.result()["rejected"] is True
    finally:
        server.close()


def test_server_keeps_working_after_worker_failure(tmp_path):
    calls = {"n": 0}

    def execute(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first task explodes")
        return {"accepted": True, "result": {"n": calls["n"]}}

    server = make_server(tmp_path, CallableExecutor(execute),
                         max_retries=0)
    try:
        first = server.submit_task("demo", "Exploding task")
        failed = wait_for_status(server, first.task_id, {TaskStatus.FAILED})
        assert failed.status == TaskStatus.FAILED
        # The pool and scheduler survived: the next task runs normally.
        second = server.submit_task("demo", "Follow-up task")
        done = wait_for_status(server, second.task_id,
                               {TaskStatus.COMPLETED})
        assert done.status == TaskStatus.COMPLETED
        assert server.pool.alive
        assert server.scheduler.running
    finally:
        server.close()


def test_pre_run_checkpoint_is_recorded(tmp_path):
    server = make_server(tmp_path, success_executor())
    try:
        task = server.submit_task("demo", "Checkpointed work")
        done = wait_for_status(server, task.task_id,
                               {TaskStatus.COMPLETED})
        assert done.checkpoint_id
        events, _ = server.events.list(task.task_id, limit=500)
        types = event_types([e.to_dict() for e in events])
        assert "checkpoint.created" in types
    finally:
        server.close()


def test_per_project_concurrency_cap(tmp_path):
    blocker = BlockingExecutor()
    other_root = tmp_path / "other"
    other_root.mkdir()
    server = make_server(
        tmp_path, blocker.as_executor(),
        extra_projects={"other": str(other_root)},
        max_tasks_per_project=1, max_workers=4, with_repo=False)
    try:
        first = server.submit_task("demo", "one")
        second = server.submit_task("demo", "two")
        assert blocker.entered.wait(timeout=10)
        # Per-project cap: while one demo task runs, the next stays queued.
        deadline = time.time() + 2
        while time.time() < deadline:
            statuses = {
                server.tasks.get(first.task_id).status,
                server.tasks.get(second.task_id).status}
            if statuses == {TaskStatus.RUNNING, TaskStatus.QUEUED}:
                break
            time.sleep(0.05)
        assert server.tasks.get(first.task_id).status == TaskStatus.RUNNING
        assert server.tasks.get(second.task_id).status == TaskStatus.QUEUED
        assert blocker.calls == 1

        # A different project is not blocked by demo's cap.
        other = server.submit_task("other", "three")
        assert wait_until(lambda: blocker.calls == 2, timeout=10)
        assert server.tasks.get(other.task_id).status == TaskStatus.RUNNING

        blocker.release.set()
        for task_id in (first.task_id, second.task_id, other.task_id):
            wait_for_status(server, task_id, {TaskStatus.COMPLETED},
                            timeout=20)
    finally:
        blocker.release.set()
        server.close()


def test_scheduler_dispatches_after_wake(tmp_path):
    gate = threading.Event()

    def execute(ctx):
        gate.wait(timeout=10)
        return {"accepted": True, "result": {}}

    server = make_server(tmp_path, CallableExecutor(execute),
                         poll_interval=5.0)  # slow poll: wake() must matter
    try:
        task = server.submit_task("demo", "wakeup")
        # submit_task wakes the scheduler; despite the 5s poll the task
        # starts almost immediately.
        started = wait_for_status(server, task.task_id,
                                  {TaskStatus.RUNNING, TaskStatus.STARTED},
                                  timeout=3)
        assert started.status in (TaskStatus.STARTED, TaskStatus.RUNNING)
        gate.set()
        wait_for_status(server, task.task_id, {TaskStatus.COMPLETED})
    finally:
        gate.set()
        server.close()


def test_worker_stage_and_progress_updates(tmp_path):
    def execute(ctx):
        ctx.set_stage("PLAN")
        ctx.set_stage("CODE")
        ctx.set_stage("COMMIT")
        return {"accepted": True, "result": {}}

    server = make_server(tmp_path, CallableExecutor(execute))
    try:
        task = server.submit_task("demo", "staged work")
        done = wait_for_status(server, task.task_id,
                               {TaskStatus.COMPLETED})
        events, _ = server.events.list(task.task_id, limit=500)
        stages = [e.data["stage"] for e in events
                  if e.type == "stage.started"]
        assert stages[:3] == ["planning", "coding", "commit"]
        assert done.progress == 1.0
    finally:
        server.close()


def test_worker_never_raises_out_of_pool(tmp_path):
    def execute(ctx):
        # Even a non-Exception failure mode must not escape the worker.
        raise ValueError("bad executor")

    server = make_server(tmp_path, CallableExecutor(execute),
                         max_retries=0)
    try:
        task = server.submit_task("demo", "poison")
        failed = wait_for_status(server, task.task_id, {TaskStatus.FAILED})
        assert "bad executor" in failed.error
        assert server.running
    finally:
        server.close()
