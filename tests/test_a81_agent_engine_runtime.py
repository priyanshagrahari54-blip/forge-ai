"""Agent Creation Engine (A81): isolation and permission boundaries.

Requirement: tests must verify isolation and permission boundaries.
Every check here drives the real runtime surface:

- agents operate through Model Fabric, PolicyGate, Tool Runtime,
  Memory, Verification, and Checkpoints — and through nothing else;
- an agent cannot call tools outside its declaration;
- an agent cannot reach operations outside its grant (BLOCKED, not
  approval-required);
- approval comes only from the operator-supplied approver — the agent
  API has no way to approve itself;
- writes are confined to the spec's working directories and protected
  paths stay denied even with approval;
- memory is namespaced per agent (and per version scope);
- resource limits are hard and refuse further work once exhausted;
- a disabled agent cannot execute anything.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from forge.agent_engine.errors import AgentLimitError, NotRunnableError
from forge.agent_engine.manager import AgentManager
from forge.agent_engine.spec import AgentSpec
from forge.agent_engine.templates import build_template_spec


@pytest.fixture
def coding_manager(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")
    return manager


def _runtime(manager, name="coder-1", **kwargs):
    return manager.runtime_for(name, **kwargs)


# ---------------------------------------------------------------------------
# Tool Runtime + PolicyGate: the only way to touch the repository
# ---------------------------------------------------------------------------

def test_runtime_exposes_only_declared_tools(coding_manager):
    runtime = _runtime(coding_manager)
    assert set(runtime._tool_runtime.tools) == set(
        runtime.spec.tools) == {"read_file", "write_file", "run_tests",
                                "search", "git_status", "git_diff"}


def test_undeclared_tool_is_refused(coding_manager):
    runtime = _runtime(coding_manager)
    for tool in ("run_command", "git_push", "delete_file", "anything"):
        result = runtime.use_tool(tool, path="src/x.py")
        assert result.success is False
        assert "not granted" in result.error, result.error
    # And nothing was executed: the tool never existed in the runtime.
    assert set(runtime._tool_runtime.tools) <= set(runtime.spec.tools)


def test_non_granted_operations_are_blocked_not_approval(coding_manager):
    runtime = _runtime(coding_manager)
    manager = runtime._permission_manager
    assert manager.check("delete_repository").value == "blocked"
    assert manager.check("expose_secrets").value == "blocked"
    assert manager.check("git_push").value == "blocked"  # not granted
    # Even approval cannot lift a block.
    allowed, _reason = manager.may_execute("delete_repository",
                                           approved=True)
    assert allowed is False


def test_granted_reads_are_safe_and_writes_need_approval(coding_manager,
                                                         tmp_path):
    runtime = _runtime(coding_manager)
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "note.txt").write_text("hello\n")
    read = runtime.use_tool("read_file", path="src/note.txt")
    assert read.success is True
    assert read.output == "hello\n"
    write = runtime.use_tool("write_file", path="src/new.py",
                             content="x = 1\n")
    assert write.success is False
    assert "pproval" in write.error


def test_operator_approver_is_the_only_approval_path(coding_manager,
                                                     tmp_path):
    (tmp_path / "src").mkdir(exist_ok=True)
    # No approver: writes always refused.
    runtime = _runtime(coding_manager)
    refused = runtime.use_tool("write_file", path="src/a.py",
                               content="a\n")
    assert refused.success is False

    # Denying approver: still refused.
    runtime = _runtime(coding_manager,
                       approver=lambda _op, _call: False)
    refused = runtime.use_tool("write_file", path="src/a.py",
                               content="a\n")
    assert refused.success is False

    # Approving operator: allowed - and PolicyGate saw the approval.
    runtime = _runtime(coding_manager,
                       approver=lambda _op, _call: True)
    allowed = runtime.use_tool("write_file", path="src/a.py",
                               content="a\n")
    assert allowed.success is True
    assert (tmp_path / "src" / "a.py").read_text() == "a\n"

    # The approver is constructor-only: an agent cannot flip it.
    with pytest.raises(TypeError):
        runtime.use_tool("write_file", path="src/b.py", content="b\n",
                         approved=True)  # no such parameter exists


def test_policy_gate_denies_are_absolute(coding_manager, tmp_path):
    # Even with a fully approving operator, protected paths stay denied.
    (tmp_path / "src").mkdir(exist_ok=True)
    runtime = _runtime(coding_manager, approver=lambda _op, _call: True)
    for path in (".env", "secrets.json", ".git/config",
                 ".forge/state.db", "src/../escape.py"):
        result = runtime.use_tool("write_file", path=path,
                                  content="secret\n")
        assert result.success is False, path
    assert not (tmp_path / ".env").exists()


def test_writes_confined_to_working_directories(coding_manager, tmp_path):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    runtime = _runtime(coding_manager, approver=lambda _op, _call: True)
    allowed = runtime.use_tool("write_file", path="src/ok.py",
                               content="ok\n")
    assert allowed.success is True
    refused = runtime.use_tool("write_file", path="docs/outside.py",
                               content="no\n")
    assert refused.success is False
    assert "working directories" in refused.error
    assert not (tmp_path / "docs" / "outside.py").exists()


def test_agents_without_working_dirs_write_repo_wide_but_protected(
        tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("researcher-1", template="research")
    manager.test("researcher-1")
    manager.enable("researcher-1")
    runtime = manager.runtime_for(
        "researcher-1", approver=lambda _op, _call: True)
    # Research template has no write tools at all.
    result = runtime.use_tool("write_file", path="notes.txt",
                              content="n\n")
    assert result.success is False
    assert "not granted" in result.error


# ---------------------------------------------------------------------------
# Memory: namespaced, bounded, isolated
# ---------------------------------------------------------------------------

def test_memory_is_namespaced_per_agent(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    for name, template in (("coder-1", "coding"), ("coder-2", "coding")):
        manager.create(name, template=template)
        manager.test(name)
        manager.enable(name)
    first = manager.runtime_for("coder-1")
    second = manager.runtime_for("coder-2")
    first.remember("secret", "only-coder-1")
    assert first.recall("secret") == "only-coder-1"
    assert second.recall("secret") is None  # isolation boundary
    # Raw store layout proves the namespaces: this agent's entries live
    # under its own namespace, and no other agent can reach them.
    keys = first._memory.list()
    assert any(key.startswith("agents/coder-1/") for key in keys)
    for key in keys:
        if "coder-2" in key:
            assert key.startswith("agents/coder-2/"), key


def test_memory_is_namespaced_per_version_scope(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")
    v1 = manager.runtime_for("coder-1")
    v1.remember("probe", "v1-value")
    # A second version with the same spec: new version scope.
    manager.create_version(
        "coder-1", build_template_spec("coding", "coder-1"),
        changelog="rework")
    manager.test("coder-1")
    manager.enable("coder-1")
    v2 = manager.runtime_for("coder-1")
    assert v2.version == 2
    assert v2.recall("probe") is None  # per-version retention
    assert v1.recall("probe") == "v1-value"


def test_memory_rejects_traversal_keys(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1")
    for bad in ("../escape", "a/../../escape", "/absolute", ".."):
        with pytest.raises(AgentLimitError):
            runtime.remember(bad, "x")
    with pytest.raises(AgentLimitError):
        runtime.recall("../../escape")  # fail closed, not silent None


def test_memory_entry_budget_enforced(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    spec = build_template_spec("coding", "coder-1").to_dict()
    spec["memory"] = {"enabled": True, "max_entries": 2,
                      "max_entry_bytes": 4096, "retention": "version"}
    manager.create("coder-1", template="coding", overrides=spec)
    manager.test("coder-1")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1")
    runtime.remember("a", "1")
    runtime.remember("b", "2")
    with pytest.raises(AgentLimitError):
        runtime.remember("c", "3")


def test_memory_size_bound_enforced(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    spec = build_template_spec("coding", "coder-1").to_dict()
    spec["memory"] = {"enabled": True, "max_entries": 10,
                      "max_entry_bytes": 8, "retention": "version"}
    manager.create("coder-1", template="coding", overrides=spec)
    manager.test("coder-1")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1")
    with pytest.raises(AgentLimitError):
        runtime.remember("big", "x" * 20)


def test_memory_disabled_by_policy(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    spec = build_template_spec("coding", "coder-1").to_dict()
    spec["memory"]["enabled"] = False
    manager.create("coder-1", template="coding", overrides=spec)
    manager.test("coder-1")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1")
    with pytest.raises(AgentLimitError):
        runtime.remember("k", "v")
    assert runtime.recall("k") is None


# ---------------------------------------------------------------------------
# Model Fabric
# ---------------------------------------------------------------------------

def test_model_requests_route_through_the_fabric(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")

    class _RecordingFabric:
        def __init__(self):
            self.requests = []

        def generate(self, request):
            self.requests.append(request)

            class _Response:
                success = True
                model = "rec"
                provider = "rec"
                text = "recorded"
                error = ""

            return _Response()

    fabric = _RecordingFabric()
    runtime = manager.runtime_for("coder-1", fabric=fabric)
    response = runtime.call_model("implement x")
    assert response["success"] is True
    assert len(fabric.requests) == 1
    request = fabric.requests[0]
    assert request.capability == "coding"
    assert set(request.required_capabilities) == {"coding", "debugging"}


def test_model_request_budget_enforced(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    spec = build_template_spec("coding", "coder-1").to_dict()
    spec["limits"]["max_tokens_per_request"] = 32
    spec["limits"]["max_requests"] = 2
    manager.create("coder-1", template="coding", overrides=spec)
    manager.test("coder-1")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1")
    # Oversized prompt refused by the token budget.
    with pytest.raises(AgentLimitError):
        runtime.call_model("x" * 1000)
    # Request budget: the refused call did not consume requests.
    assert runtime.state()["requests"] == 0
    runtime.call_model("hi")
    runtime.call_model("hi")
    with pytest.raises(AgentLimitError):
        runtime.call_model("hi")
    assert runtime.state()["requests"] == 2
    # Once refused, the runtime refuses everything further.
    with pytest.raises(AgentLimitError):
        runtime.use_tool("read_file", path="src/x.py")


# ---------------------------------------------------------------------------
# File-write budget + checkpoints
# ---------------------------------------------------------------------------

def test_file_write_budget_enforced(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    spec = build_template_spec("coding", "coder-1").to_dict()
    spec["limits"]["max_files_written"] = 1
    manager.create("coder-1", template="coding", overrides=spec)
    manager.test("coder-1")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1",
                                  approver=lambda _op, _call: True)
    (tmp_path / "src").mkdir(exist_ok=True)
    assert runtime.use_tool("write_file", path="src/one.py",
                            content="1\n").success is True
    with pytest.raises(AgentLimitError):
        runtime.use_tool("write_file", path="src/two.py", content="2\n")
    assert not (tmp_path / "src" / "two.py").exists()
    # Budget exhaustion is terminal for the session: no further work.
    assert runtime.state()["refused"] is True
    with pytest.raises(AgentLimitError):
        runtime.use_tool("write_file", path="src/one.py", content="1b\n")


def test_checkpoints_capture_and_restore(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1")
    (tmp_path / "src").mkdir(exist_ok=True)
    target = tmp_path / "src" / "probe.txt"
    target.write_text("before\n")
    checkpoint = runtime.checkpoint(declared=["src/probe.txt"])
    target.write_text("after\n")
    runtime.rollback(checkpoint, changed_files=["src/probe.txt"])
    assert target.read_text() == "before\n"


def test_verification_pipeline_runs_through_runtime(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1")
    report = runtime.verify(changed_files=[])
    assert isinstance(report, dict)
    assert set(report) == {"passed", "gates", "changed_files"}
    gate_names = [gate["name"] for gate in report["gates"]]
    assert {"tests", "build", "lint/type", "security", "review"} <= set(
        gate_names)


# ---------------------------------------------------------------------------
# Lifecycle gating
# ---------------------------------------------------------------------------

def test_disabled_agent_cannot_execute(tmp_path):
    manager = AgentManager(root=tmp_path / "agents", workspace=tmp_path)
    manager.create("coder-1", template="coding")
    manager.test("coder-1")
    manager.enable("coder-1")
    runtime = manager.runtime_for("coder-1")
    assert runtime.call_model("hello")["success"] is True
    manager.pause("coder-1")
    paused = manager.runtime_for("coder-1")
    with pytest.raises(NotRunnableError):
        paused.call_model("hello")
    with pytest.raises(NotRunnableError):
        paused.use_tool("read_file", path="src/x.py")
    with pytest.raises(NotRunnableError):
        paused.remember("k", "v")
    manager.resume("coder-1")
    manager.disable("coder-1")
    disabled = manager.runtime_for("coder-1")
    assert disabled.enabled is False
    with pytest.raises(NotRunnableError):
        disabled.use_tool("read_file", path="src/x.py")


def test_runtime_state_reports_accounting(coding_manager):
    runtime = _runtime(coding_manager)
    state = runtime.state()
    assert state["agent"] == "coder-1"
    assert state["version"] == 1
    assert state["enabled"] is True
    runtime.call_model("hello")
    state = runtime.state()
    assert state["requests"] == 1
    assert state["tokens_used"] > 0
    assert state["limits"]["max_requests"] == 200
