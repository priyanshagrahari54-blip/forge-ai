"""A81 supervisor integration: A33 security and A32 atomicity.

``Supervisor.execute_parallel`` wraps the parallel scheduler with:

* the A33 permission gate on every dispatch (``Resource.AGENT /
  execute``) and every declared file read/write (``read_file`` /
  ``write_file`` through the A32 PolicyGate). DENY fails the task
  closed (DENIED, never retried); REQUIRE_APPROVAL is resolved by the
  operator callback and redeems a single-use token;
* A32 ChangeSet atomicity: a checkpoint is created before the first
  dispatch and a FAILED / CANCELLED run rolls back exactly the files
  declared by succeeded tasks — no partial change set survives.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import make_repo  # noqa: E402

from forge.core.run_control import SupervisorControl  # noqa: E402
from forge.core.supervisor import Supervisor  # noqa: E402
from forge.orchestration import TaskGraph  # noqa: E402
from forge.security.policy import (PermissionPolicy, PermissionRule,  # noqa: E402
                                   Resource)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    make_repo(root)
    return root


def allow_policy():
    return PermissionPolicy(rules=[
        PermissionRule(id="agents", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="reads", resource=Resource.FILESYSTEM,
                       operation="read", scope="**", effect="ALLOW"),
        PermissionRule(id="writes", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])


def _write_worker(root, name):
    def worker(w):
        target = Path(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{name} content\n")
        return {"files": [name], "summary": f"wrote {name}"}
    return worker


def test_parallel_run_succeeds_and_keeps_changes(repo):
    g = TaskGraph()
    g.add_task("a", "write one", "coder", writes=["a.txt"])
    g.add_task("b", "write two", "debugger", writes=["b.txt"])
    sup = Supervisor("demo", root=repo)
    result = sup.execute_parallel(
        "build two files", g,
        {"coder": _write_worker(repo, "a.txt"),
         "debugger": (lambda w: (
             (repo / "b.txt").write_text("b\n"),
             {"root_cause": "n/a", "files": ["b.txt"],
              "summary": "wrote b"})[1])},
        mode="autonomous", policy=allow_policy())
    assert result["status"] == "SUCCEEDED"
    assert result["accepted"] is True
    assert result["rollback"] is False
    assert result["files"] == ["a.txt", "b.txt"]
    assert (repo / "a.txt").exists() and (repo / "b.txt").exists()
    # agent activity snapshot for the desktop views
    states = {a["role"]: a["state"]
              for a in result["activity"]["agents"]}
    assert states["coder"] == "succeeded"
    assert states["debugger"] == "succeeded"


def test_denied_dispatch_fails_closed_and_rolls_back(repo):
    deny = PermissionPolicy(rules=[
        PermissionRule(id="no-debugger", resource=Resource.AGENT,
                       operation="execute", scope="debugger",
                       effect="DENY"),
        PermissionRule(id="agents", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="reads", resource=Resource.FILESYSTEM,
                       operation="read", scope="**", effect="ALLOW"),
        PermissionRule(id="writes", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])
    # b.txt starts with known content; the denied task must not write it.
    (repo / "b.txt").write_text("original\n")
    g = TaskGraph()
    g.add_task("a", "write one", "coder", writes=["a.txt"])
    g.add_task("b", "write two", "debugger", writes=["b.txt"])
    sup = Supervisor("demo", root=repo)
    result = sup.execute_parallel(
        "deny test", g,
        {"coder": _write_worker(repo, "a.txt"),
         "debugger": _write_worker(repo, "b.txt")},
        mode="autonomous", policy=deny)
    assert result["status"] == "FAILED"
    assert result["accepted"] is False
    statuses = {t["id"]: t["status"] for t in result["tasks"]}
    assert statuses["b"] == "DENIED"
    b = next(t for t in result["tasks"] if t["id"] == "b")
    assert "denied" in b["error"].lower()
    assert b["attempts"] == 1  # denials are never retried
    # A32 atomicity: the run failed, so the successful file is rolled
    # back and the worktree is as it was.
    assert result["rollback"] is True
    assert not (repo / "a.txt").exists()
    assert (repo / "b.txt").read_text() == "original\n"


def test_denied_write_file_fails_closed(repo):
    deny_writes = PermissionPolicy(rules=[
        PermissionRule(id="agents", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="reads", resource=Resource.FILESYSTEM,
                       operation="read", scope="**", effect="ALLOW"),
        PermissionRule(id="deny-a", resource=Resource.FILESYSTEM,
                       operation="write", scope="a.txt",
                       effect="DENY"),
        PermissionRule(id="writes", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])
    g = TaskGraph()
    g.add_task("a", "write one", "coder", writes=["a.txt"])
    sup = Supervisor("demo", root=repo)
    result = sup.execute_parallel(
        "deny write", g, {"coder": _write_worker(repo, "a.txt")},
        mode="autonomous", policy=deny_writes)
    assert result["status"] == "FAILED"
    assert result["tasks"][0]["status"] == "DENIED"
    assert not (repo / "a.txt").exists()


def test_require_approval_round_trip_with_token(repo):
    """REQUIRE_APPROVAL dispatch: the operator callback is consulted,
    the minted token is redeemed, and the task runs."""
    from forge.security.approvals import ApprovalStore

    store = ApprovalStore()
    asked = []

    def callback(query):
        """Approve by filing the request the query describes, deciding
        it, and issuing a scoped token (the A33 operator flow)."""
        from forge.security.approvals import ApprovalRequest
        items = getattr(query, "items", ()) or ()
        if items:
            item = items[0]
            request = ApprovalRequest(
                agent="coder", resource=Resource.FILESYSTEM,
                operation="write", scopes=(item.path,),
                task_id=query.task_id, reason="test approval",
                consequences="")
        else:
            request = ApprovalRequest(
                agent="forge-supervisor", resource=Resource.AGENT,
                operation="execute", scopes=(query.capability,),
                task_id=query.task_id, reason="test approval",
                consequences="")
        asked.append((request.resource.value, request.operation))
        store.submit(request)
        store.decide(request.id, True, "operator")
        token = store.issue(request.id, decided_by="operator",
                            ttl_seconds=60, max_uses=1)
        return token.id

    approval_policy = PermissionPolicy(rules=[
        PermissionRule(id="need-approval", resource=Resource.AGENT,
                       operation="execute", scope="coder",
                       effect="REQUIRE_APPROVAL"),
        PermissionRule(id="agents", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="reads", resource=Resource.FILESYSTEM,
                       operation="read", scope="**", effect="ALLOW"),
        PermissionRule(id="writes", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])
    g = TaskGraph()
    g.add_task("a", "write one", "coder", writes=["a.txt"])
    sup = Supervisor("demo", root=repo)
    result = sup.execute_parallel(
        "approval test", g, {"coder": _write_worker(repo, "a.txt")},
        policy=approval_policy, approval_store=store,
        approval_callback=callback)
    assert result["status"] == "SUCCEEDED"
    assert (repo / "a.txt").exists()
    # the operator was consulted for both the dispatch and the write
    assert ("agent", "execute") in asked
    assert ("filesystem", "write") in asked


def test_stale_token_never_authorizes(repo):
    """A callback that returns a token for a different request fails
    closed: the gate re-checks token bindings on redemption."""
    from forge.security.approvals import ApprovalRequest, ApprovalStore

    store = ApprovalStore()
    # Mint a token bound to an unrelated request...
    foreign = ApprovalRequest(
        agent="someone-else", resource=Resource.AGENT,
        operation="execute", scopes=("debugger",), task_id="other",
        reason="unrelated")
    store.submit(foreign)
    store.decide(foreign.id, True, "operator")
    token = store.issue(foreign.id, decided_by="operator", ttl_seconds=60,
                        max_uses=1)

    def callback(query):
        return token.id  # wrong token for this request

    approval_policy = PermissionPolicy(rules=[
        PermissionRule(id="need-approval", resource=Resource.AGENT,
                       operation="execute", scope="coder",
                       effect="REQUIRE_APPROVAL"),
        PermissionRule(id="agents", resource=Resource.AGENT,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="reads", resource=Resource.FILESYSTEM,
                       operation="read", scope="**", effect="ALLOW"),
        PermissionRule(id="writes", resource=Resource.FILESYSTEM,
                       operation="write", scope="**", effect="ALLOW"),
    ])
    g = TaskGraph()
    g.add_task("a", "write one", "coder", writes=["a.txt"])
    sup = Supervisor("demo", root=repo)
    result = sup.execute_parallel(
        "stale token", g, {"coder": _write_worker(repo, "a.txt")},
        mode="autonomous", policy=approval_policy, approval_store=store,
        approval_callback=callback)
    assert result["status"] == "FAILED"
    assert result["tasks"][0]["status"] == "DENIED"
    assert not (repo / "a.txt").exists()


def test_failed_run_rolls_back_all_written_files(repo):
    original = (repo / "app.py").read_text()
    g = TaskGraph()
    g.add_task("ok1", "write one", "coder", writes=["ok1.txt"])
    g.add_task("ok2", "write two", "debugger", writes=["ok2.txt"])
    g.add_task("bad", "write three", "tester", writes=["bad.txt"])

    def tester_worker(w):
        (repo / "bad.txt").write_text("bad\n")
        raise RuntimeError("tester exploded")

    sup = Supervisor("demo", root=repo)
    result = sup.execute_parallel(
        "fail", g,
        {"coder": _write_worker(repo, "ok1.txt"),
         "debugger": (lambda w: {
             "root_cause": "", "files": ["ok2.txt"], "summary": "ok"}),
         "tester": tester_worker},
        mode="autonomous", policy=allow_policy())
    assert result["status"] == "FAILED"
    assert result["rollback"] is True
    # every file written by any succeeded task is gone again
    for name in ("ok1.txt", "ok2.txt", "bad.txt"):
        assert not (repo / name).exists(), name
    assert (repo / "app.py").read_text() == original


def test_cancelled_run_rolls_back(repo):
    import time

    control = SupervisorControl()
    g = TaskGraph()
    g.add_task("a", "write one", "coder", writes=["a.txt"])
    g.add_task("b", "slow", "planner")

    def slow(w):
        time.sleep(0.3)
        return {"plan": [], "requirements": "", "summary": ""}

    def cancel_soon():
        time.sleep(0.05)
        control.request_cancel()
    import threading
    threading.Thread(target=cancel_soon).start()
    sup = Supervisor("demo", root=repo)
    result = sup.execute_parallel(
        "cancel", g,
        {"coder": _write_worker(repo, "a.txt"), "planner": slow},
        max_workers=2, mode="autonomous", policy=allow_policy(),
        control=control, retry_backoff=0.0)
    assert result["status"] == "CANCELLED"
    assert result["cancelled"] is True
    assert result["rollback"] is True
    # whatever the cancelled run managed to write is rolled back
    assert not (repo / "a.txt").exists()


def test_execution_state_store_receives_journal(repo, tmp_path):
    from forge.orchestration import ExecutionStateStore

    store = ExecutionStateStore(tmp_path / "exec.db")
    g = TaskGraph()
    g.add_task("a", "write one", "coder", writes=["a.txt"])
    sup = Supervisor("demo", root=repo)
    result = sup.execute_parallel(
        "journal", g, {"coder": _write_worker(repo, "a.txt")},
        mode="autonomous", policy=allow_policy(), state_store=store,
        run_id="exec-1")
    assert result["status"] == "SUCCEEDED"
    loaded = store.load_run("exec-1")
    assert loaded["run"]["status"] == "SUCCEEDED"
    assert {t["id"] for t in loaded["tasks"]} == {"a"}
    assert loaded["messages"]
    assert loaded["events"]


def test_graph_must_be_a_task_graph(repo):
    sup = Supervisor("demo", root=repo)
    with pytest.raises(TypeError):
        sup.execute_parallel("x", "not a graph", {})


def test_conflicting_tasks_are_serialized_by_the_supervisor(repo):
    """Two tasks writing the same file through the supervisor: both
    succeed, and the file contains exactly one writer's content (they
    never interleaved)."""
    import threading
    import time

    active = []
    lock = threading.Lock()
    peak = [0]

    g = TaskGraph()
    g.add_task("w1", "write shared", "coder", writes=["shared.txt"])
    g.add_task("w2", "write shared again", "debugger",
               writes=["shared.txt"])

    def writer1(w):
        with lock:
            active.append("w1")
            peak[0] = max(peak[0], len(active))
        time.sleep(0.05)
        with lock:
            active.remove("w1")
        (repo / "shared.txt").write_text("one\n")
        return {"files": ["shared.txt"], "summary": "one"}

    def writer2(w):
        with lock:
            active.append("w2")
            peak[0] = max(peak[0], len(active))
        time.sleep(0.05)
        with lock:
            active.remove("w2")
        (repo / "shared.txt").write_text("two\n")
        return {"root_cause": "", "files": ["shared.txt"], "summary": "two"}

    sup = Supervisor("demo", root=repo)
    result = sup.execute_parallel(
        "conflict", g, {"coder": writer1, "debugger": writer2},
        max_workers=2, mode="autonomous", policy=allow_policy())
    assert result["status"] == "SUCCEEDED"
    assert peak[0] == 1  # the writers never overlapped
    # last writer wins, deterministically (they were serialized)
    assert (repo / "shared.txt").read_text() in ("one\n", "two\n")
    events = [e for e in result["events"]
              if e["name"] == "conflict_serialized"]
    assert events
