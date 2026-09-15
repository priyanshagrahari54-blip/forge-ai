"""Session 11.5 (§8) — one attempt authority: lease + fence + control.

Every test here is labelled. The only double used is the queue lease (a
two-line stand-in for a SQLite row); the fence registry, the attempt fences and
the SupervisorControl are the real production classes.

Labels used: SECURITY_TEST (authorization decisions), INTEGRATION_TEST (real
worker/server classes end to end), MOCK (the lease double).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers_server import make_server  # noqa: E402

from forge.core.authority import (  # noqa: E402
    DENY_CANCELLED_ATTEMPT, DENY_FENCE_ERROR, DENY_LEASE_LOST,
    DENY_NO_FENCE_AUTHORITY, DENY_STALE_ATTEMPT, DENY_SUPERSEDED_ATTEMPT,
    FENCE_ABSENT, ExecutionAuthority, authority_for)
from forge.core.fencing import FenceRegistry  # noqa: E402
from forge.core.run_control import SupervisorControl  # noqa: E402
from forge.server import TaskStatus  # noqa: E402
from forge.server.workers import run_task  # noqa: E402


class LeaseDouble:
    """MOCK — the queue's lease answer, and nothing else."""

    def __init__(self, held: bool = True, raises: bool = False) -> None:
        self.held = bool(held)
        self.raises = bool(raises)
        self.calls: List[str] = []

    def lease_held_by(self, task_id: str, owner: str) -> bool:
        self.calls.append("%s/%s" % (task_id, owner))
        if self.raises:
            raise RuntimeError("lease table unreadable")
        return self.held


class RaisingRegistry:
    """MOCK — a fence authority that fails while being consulted (§9)."""

    def current(self, task_id: str) -> Any:
        raise RuntimeError("registry unavailable")

    def is_authorized(self, fence: Any) -> bool:
        raise RuntimeError("registry unavailable")

    def get(self, task_id: str, generation: int) -> Any:
        raise RuntimeError("registry unavailable")

    def generation_of(self, task_id: str) -> int:
        raise RuntimeError("registry unavailable")

    def cancel(self, task_id: str) -> Any:
        raise RuntimeError("registry unavailable")


class RaisingControl:
    """MOCK — a control whose cancel flag cannot be read."""

    @property
    def cancel_requested(self) -> bool:
        raise RuntimeError("control state unreadable")

    def request_cancel(self) -> None:
        raise RuntimeError("control state unreadable")


def _running(registry: FenceRegistry, task_id: str = "task-a",
             owner: str = "worker-A") -> Any:
    fence = registry.begin(task_id, owner=owner)
    return registry.mark_running(task_id, fence)


# ---------------------------------------------------------------------------
# The truth table: one question, three authorities, fail closed
# ---------------------------------------------------------------------------


def test_publish_authority_requires_every_layer_to_agree():
    """SECURITY_TEST (§8) — lease AND fence AND no cancellation, or denial."""
    registry = FenceRegistry()
    fence = _running(registry)
    control = SupervisorControl()
    lease = LeaseDouble(held=True)

    authority = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                                   fence=fence, registry=registry, queue=lease,
                                   control=control)
    assert authority.publish_authorized() is True
    assert authority.mutation_authorized() is True
    assert authority.denial_reason() == ""
    assert authority.fence_state == "RUNNING"
    assert authority.cancellation_epoch == 0

    #: the lease alone is not permission
    lease.held = False
    assert authority.publish_authorized() is False
    assert authority.denial_reason() == DENY_LEASE_LOST
    lease.held = True

    #: neither is a fence without a cancellation check
    control.request_cancel()
    assert authority.publish_authorized() is False
    assert authority.denial_reason() == DENY_CANCELLED_ATTEMPT
    assert authority.cancellation_epoch == 1
    control2 = SupervisorControl()
    authority = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                                   fence=fence, registry=registry, queue=lease,
                                   control=control2)
    registry.cancel("task-a")                      # RUNNING -> CANCELLING
    assert authority.denial_reason() == DENY_CANCELLED_ATTEMPT
    assert authority.fence_state == "CANCELLING"


def test_a_superseded_attempt_is_named_superseded_not_just_stale():
    """SECURITY_TEST (§8/§12) — the registry's view wins over the stale copy."""
    registry = FenceRegistry()
    stale = _running(registry, owner="worker-A")
    successor = registry.begin("task-a", owner="worker-B")
    registry.mark_running("task-a", successor)

    old = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                             fence=stale, registry=registry,
                             queue=LeaseDouble(held=True))
    new = ExecutionAuthority(task_id="task-a", lease_owner="worker-B",
                             fence=successor, registry=registry,
                             queue=LeaseDouble(held=True))
    #: the frozen record the zombie holds still says RUNNING…
    assert stale.state == "RUNNING"
    #: …and the authority refuses it anyway, naming the real diagnosis
    assert old.publish_authorized() is False
    assert old.denial_reason() == DENY_SUPERSEDED_ATTEMPT
    assert new.publish_authorized() is True
    assert new.generation == 2
    assert old.generation == 1


def test_a_terminal_attempt_may_neither_write_nor_publish():
    """SECURITY_TEST (§8/§16) — SUCCEEDED is not a licence to write again."""
    registry = FenceRegistry()
    fence = _running(registry)
    lease = LeaseDouble(held=True)
    authority = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                                   fence=fence, registry=registry, queue=lease)
    registry.commit("task-a", fence, "SUCCEEDED", payload={"files": 1})
    assert authority.publish_authorized() is False
    assert authority.denial_reason() == DENY_STALE_ATTEMPT
    assert authority.mutation_authorized() is False
    assert authority.fence_state == "SUCCEEDED"


def test_uncertainty_is_denial_never_permission():
    """SECURITY_TEST (§9) — an unreadable lease, registry or control denies."""
    registry = FenceRegistry()
    fence = _running(registry)

    unreadable_lease = ExecutionAuthority(
        task_id="task-a", lease_owner="worker-A", fence=fence,
        registry=registry, queue=LeaseDouble(raises=True))
    assert unreadable_lease.publish_authorized() is False
    assert unreadable_lease.denial_reason() == DENY_LEASE_LOST

    broken_registry = ExecutionAuthority(
        task_id="task-a", lease_owner="worker-A", fence=fence,
        registry=RaisingRegistry(), queue=LeaseDouble(held=True))
    assert broken_registry.publish_authorized() is False
    assert broken_registry.denial_reason() == DENY_FENCE_ERROR

    unreadable_control = ExecutionAuthority(
        task_id="task-a", lease_owner="worker-A", fence=fence,
        registry=registry, queue=LeaseDouble(held=True),
        control=RaisingControl())
    assert unreadable_control.publish_authorized() is False
    assert unreadable_control.denial_reason() == DENY_CANCELLED_ATTEMPT
    assert unreadable_control.cancel_requested is True


def test_no_fence_with_a_deployed_registry_is_denied():
    """SECURITY_TEST (§9) — 'we could not establish authority' is a denial."""
    registry = FenceRegistry()
    strict = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                                fence=None, registry=registry,
                                queue=LeaseDouble(held=True))
    assert strict.publish_authorized() is False
    assert strict.denial_reason() == DENY_NO_FENCE_AUTHORITY
    assert strict.fence_state == FENCE_ABSENT
    assert strict.snapshot()["require_fence"] is True

    #: an explicit opt-out is honoured, and recorded rather than hidden
    legacy = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                                fence=None, registry=registry,
                                queue=LeaseDouble(held=True),
                                require_fence=False)
    assert legacy.publish_authorized() is True
    assert legacy.snapshot()["require_fence"] is False

    #: no registry deployed at all: the lease is the only authority that exists
    no_registry = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                                     fence=None, registry=None,
                                     queue=LeaseDouble(held=True))
    assert no_registry.publish_authorized() is True
    assert no_registry.snapshot()["fence_authority"] == "absent"


def test_the_commit_guard_names_the_reason_it_refuses():
    """SECURITY_TEST (§8/§10/§28) — the write choke points get one guard.

    The guard is the existing fencing contract (``""`` while authorized, a
    reason once not) widened with the lease and the cancellation flag, so a tool
    cannot write through a gap between the three authorities.
    """
    registry = FenceRegistry()
    fence = _running(registry)
    lease = LeaseDouble(held=True)
    authority = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                                   fence=fence, registry=registry, queue=lease)
    guard = authority.commit_guard()
    assert guard() == ""

    lease.held = False
    reason = guard()
    assert reason and DENY_LEASE_LOST in reason
    assert "task-a#g1" in reason
    lease.held = True
    assert guard() == ""

    registry.fence("task-a", fence, reason="restart")
    assert DENY_STALE_ATTEMPT in guard()


def test_request_cancel_is_one_chain_and_idempotent():
    """SECURITY_TEST (§11) — cancel reaches the control and the fence at once."""
    registry = FenceRegistry()
    fence = _running(registry)
    control = SupervisorControl()
    authority = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                                   fence=fence, registry=registry,
                                   queue=LeaseDouble(held=True),
                                   control=control)
    assert authority.request_cancel("operator asked") is True
    assert control.cancel_requested is True
    assert registry.current("task-a").state == "CANCELLING"
    assert authority.publish_authorized() is False

    #: asking twice is normal, not a state conflict
    assert authority.request_cancel("again") is True
    assert registry.current("task-a").state == "CANCELLING"

    #: a control that cannot be signalled still leaves the fence revoked
    tough = ExecutionAuthority(task_id="task-a", lease_owner="worker-A",
                               fence=fence, registry=registry,
                               queue=LeaseDouble(held=True),
                               control=RaisingControl())
    assert tough.request_cancel("x") is True
    assert tough.publish_authorized() is False


def test_the_snapshot_is_bounded_and_free_of_payloads():
    """SECURITY_TEST (§33) — observability without prompts, paths or results."""
    registry = FenceRegistry()
    fence = _running(registry)
    authority = ExecutionAuthority(
        task_id="task-a", project_id="demo", lease_owner="worker-A",
        boot_id="boot-1", fence=fence, registry=registry,
        queue=LeaseDouble(held=True))
    snapshot: Dict[str, Any] = authority.snapshot()
    assert snapshot["task_id"] == "task-a"
    assert snapshot["attempt_id"] == "task-a#g1"
    assert snapshot["generation"] == 1
    assert snapshot["lease_checked"] is True
    assert snapshot["fence_authority"] == "registry"
    assert snapshot["publish_authorized"] is True
    blob = json.dumps(snapshot).lower()
    for forbidden in ("prompt", "result", "requirement", "/home", "token"):
        assert forbidden not in blob
    #: reading it changed nothing
    assert authority.publish_authorized() is True


def test_the_server_composes_the_same_authority_for_a_reader(tmp_path):
    """INTEGRATION_TEST (§8) — a reader is answered about the real lease owner."""
    server = make_server(tmp_path, start=False)
    try:
        task = server.submit_task("demo", "authority read", mode="assisted")
        task_id = task.task_id
        fence = server.fences.begin(task_id, owner="worker-A")
        server.fences.mark_running(task_id, fence)

        #: nobody holds the lease yet: a fence alone is not permission
        denied = server.authority_for(task_id, fence=fence)
        assert denied.publish_authorized() is False
        assert denied.denial_reason() == DENY_LEASE_LOST

        assert server.queue.lease_next("demo", "worker-A") == task_id
        #: the server resolves the owner itself, so the answer is about the
        #: attempt that really runs — not about an empty owner string
        reader = server.authority_for(task_id, fence=fence,
                                      lease_owner=server.queue.lease_owner(
                                          task_id))
        assert reader.publish_authorized() is True
        assert reader.snapshot()["lease_owner"] == "worker-A"
        assert reader.snapshot()["boot_id"] == server.boot_id
    finally:
        server.stop()


def test_a_worker_refuses_to_run_without_an_attempt_authority(tmp_path,
                                                             monkeypatch):
    """INTEGRATION_TEST · SECURITY_TEST (§9) — no authority, no execution.

    When the fence registry is deployed but an attempt fence cannot be minted,
    the worker fails the task *before* generating anything: running anyway would
    spend model compute on work that could never be published, and the honest
    recovery is a bounded retry.
    """
    server = make_server(tmp_path, start=False, max_retries=2)
    executed: List[Any] = []

    def _execute(ctx: Any) -> Dict[str, Any]:
        executed.append(ctx)
        return {"accepted": True, "result": {"ok": True}, "files": []}

    monkeypatch.setattr(server.executor, "execute", _execute)

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(server.fences, "begin", _refuse)
    try:
        task = server.submit_task("demo", "needs an authority",
                                  mode="assisted")
        task_id = task.task_id
        assert server.queue.lease_next("demo", "worker-A") == task_id
        run_task(server, task_id, "worker-A")

        #: no model compute, no writes, no publication
        assert executed == []
        record = server.tasks.get_or_raise(task_id)
        assert record.status in (TaskStatus.QUEUED, TaskStatus.FAILED)
        assert record.result_json in ("", None) or "ok" not in record.result_json
        #: and the reason is recorded, not swallowed
        events, _seq = server.events.list(task_id)
        blob = json.dumps([event.data for event in events]).lower()
        log_rows, _log_seq = server.logs.list(task_id)
        logs = "\n".join(str(getattr(row, "message", row)) for row in log_rows)
        assert "authority" in blob or "authority" in logs.lower()
        assert server.fences.current(task_id) is None or \
            server.fences.current(task_id).state != "SUCCEEDED"
    finally:
        server.stop()


def test_authority_for_reads_the_live_control_and_registry(tmp_path):
    """INTEGRATION_TEST (§8/§11) — the factory cannot ignore a cancellation."""
    server = make_server(tmp_path, start=False)
    try:
        task = server.submit_task("demo", "cancel me", mode="assisted")
        task_id = task.task_id
        assert server.queue.lease_next("demo", "worker-A") == task_id
        fence = server.fences.begin(task_id, owner="worker-A")
        server.fences.mark_running(task_id, fence)
        control = SupervisorControl()
        server.register_control(task_id, control)

        authority = authority_for(server, task_id, fence=fence,
                                  lease_owner="worker-A")
        assert authority.publish_authorized() is True
        #: the control was found through the server, not passed in
        assert authority.snapshot()["fence_state"] == "RUNNING"

        server.cancel_task(task_id, actor="operator")
        after = authority_for(server, task_id, fence=fence,
                              lease_owner="worker-A")
        assert after.publish_authorized() is False
        assert after.denial_reason() == DENY_CANCELLED_ATTEMPT
        assert control.cancel_requested is True
    finally:
        server.stop()
