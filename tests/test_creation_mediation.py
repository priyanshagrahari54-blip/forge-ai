"""Creation engine mediation: isolation, permission boundaries, integrations."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import (ScriptedProvider, login, make_client,  # noqa: E402
                         make_fabric, make_plane, make_repo)

from forge.agents.agent_bench import MARKER, run_agent_benchmark  # noqa: E402
from forge.agents.creation import AgentCreationEngine  # noqa: E402
from forge.agents.mediation import (GatedAgentRuntime,  # noqa: E402
                                    MediationError)
from forge.cli import main as cli_main  # noqa: E402
from forge.security.policy import (PermissionPolicy,  # noqa: E402
                                   PermissionRule, Resource)
from forge.security.policy_gate import PolicyDecision  # noqa: E402


ALLOW_RUNS = PermissionPolicy(rules=[
    PermissionRule(id="agent-run", resource=Resource.AGENT,
                   operation="execute", scope="", effect="ALLOW"),
])


class FakeResponse:
    def __init__(self, text: str, success: bool = True) -> None:
        self.text = text
        self.success = success
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.error = "" if success else "fake failure"


class FakeFabric:
    def __init__(self, text: str = "all good, nothing to report") -> None:
        self.text = text
        self.requests: list = []

    def generate(self, request):
        self.requests.append(request)
        return FakeResponse(self.text)


class FakeToolResult:
    def __init__(self, success: bool, output: str = "",
                 error: str = "") -> None:
        self.success = success
        self.output = output
        self.error = error


class FakeToolRuntime:
    def __init__(self) -> None:
        self.calls: list = []

    def execute(self, tool_name: str, **kwargs):
        self.calls.append((tool_name, kwargs))
        return FakeToolResult(True, output="tool-ok:%s" % tool_name)


class FakeGate:
    def __init__(self, decision: str = "ALLOW") -> None:
        self.decision = decision
        self.calls: list = []

    def evaluate(self, *args, **kwargs):
        from types import SimpleNamespace

        self.calls.append((args, kwargs))
        decision = SimpleNamespace(value=self.decision,
                                   reason="fake-%s" % self.decision.lower())
        return SimpleNamespace(decision=decision,
                               reason="fake-%s" % self.decision.lower())


def grant_all(engine: AgentCreationEngine, name: str,
              approver: str = "tester") -> None:
    """Grant every requested permission (grants reset state, so test
    flows do this before validation)."""
    count = len(engine.get(name).spec.get("permissions", []))
    for index in range(count):
        engine.grant_permission(name, index, approver=approver)


def enabled_engine(name: str = "mediated-one",
                   template: str = "research") -> AgentCreationEngine:
    engine = AgentCreationEngine()
    engine.create_from_template(template, name, created_by="tester")
    grant_all(engine, name)
    engine.validate(name, actor="tester")
    report = engine.benchmark(name, actor="tester")
    assert report["passed"]
    engine.enable(name, actor="operator")
    return engine


def runtime_for(**kwargs) -> GatedAgentRuntime:
    tmp = tempfile.mkdtemp(prefix="forge-mediation-test-")
    kwargs.setdefault("memory_root", tmp)
    return GatedAgentRuntime(**kwargs)


# -- lifecycle gating -----------------------------------------------------


def test_only_enabled_agents_run():
    engine = enabled_engine()
    runtime = runtime_for(fabric=FakeFabric())
    package = engine.get("mediated-one")
    report = runtime.run(package, "summarize the repo", actor="tester")
    assert report["success"] and report["agent"] == "mediated-one"
    for state in ("created", "validated", "tested", "paused",
                  "disabled", "retired"):
        probe = {"name": package.name, "spec": package.spec,
                 "state": state}
        with pytest.raises(MediationError) as caught:
            runtime.run(probe, "summarize the repo", actor="tester")
        assert caught.value.code == "NOT_ENABLED"


def test_run_validates_requirement():
    engine = enabled_engine()
    runtime = runtime_for(fabric=FakeFabric())
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("mediated-one"), "   ", actor="tester")
    assert caught.value.code == "BAD_REQUIREMENT"


def test_no_fabric_refuses_without_fabrication():
    engine = enabled_engine()
    runtime = runtime_for()
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("mediated-one"), "write code",
                    actor="tester")
    assert caught.value.code == "NO_MODEL"


def test_model_failure_is_honest():
    engine = enabled_engine()
    fabric = FakeFabric()
    fabric.generate = lambda request: FakeResponse("", success=False)
    runtime = runtime_for(fabric=fabric)
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("mediated-one"), "write code",
                    actor="tester")
    assert caught.value.code == "MODEL_FAILED"


def test_model_request_honors_spec_bounds():
    engine = enabled_engine(name="bounded-one", template="coding")
    fabric = FakeFabric()
    runtime = runtime_for(fabric=fabric, tool_runtime=FakeToolRuntime())
    report = runtime.run(engine.get("bounded-one"), "fix it",
                         actor="tester")
    assert report["success"]
    request = fabric.requests[0]
    assert request.capability == "coding"
    assert request.min_context_window == 8192


def test_run_records_subsystem_evidence():
    engine = enabled_engine(name="evident", template="research")
    runtime = runtime_for(fabric=FakeFabric())
    report = runtime.run(engine.get("evident"), "summarize",
                         actor="tester")
    evidence = report["evidence"]
    assert evidence["lifecycle"] == "enabled"
    assert evidence["model"]["model"] == "fake-model"
    assert "read_file" in evidence["tools"]["allowlist"]
    assert evidence["tools"]["calls"] == 0
    assert evidence["memory"]["namespace"] == "agent-evident"
    assert evidence["memory"]["key"].startswith("runs/")
    assert evidence["checkpoint"]["id"] == ""  # no writes, no snapshot


# -- memory isolation -----------------------------------------------------


def test_memory_namespaces_are_isolated():
    engine = enabled_engine()
    engine.create_from_template("research", "mediated-two",
                                created_by="tester")
    engine.validate("mediated-two", actor="tester")
    engine.benchmark("mediated-two", actor="tester")
    engine.enable("mediated-two", actor="operator")
    runtime = runtime_for()
    one = engine.get("mediated-one")
    two = engine.get("mediated-two")
    runtime.write_memory(one, "notes/today.txt", "one's secret")
    assert runtime.read_memory(one, "notes/today.txt") == "one's secret"
    assert runtime.read_memory(two, "notes/today.txt") is None
    with pytest.raises(MediationError) as caught:
        runtime.read_agent_memory(one, "mediated-two", "notes/today.txt")
    assert caught.value.code == "ISOLATION"
    with pytest.raises(MediationError) as caught:
        runtime.write_agent_memory(one, "mediated-two", "evil.txt", "x")
    assert caught.value.code == "ISOLATION"


def test_memory_bounds_enforced():
    engine = AgentCreationEngine()
    engine.create_from_template(
        "research", "forgetful", created_by="t",
        overrides={"memory_policy": {"max_entries": 1,
                                     "max_bytes_per_entry": 64}})
    engine.validate("forgetful", actor="t")
    engine.benchmark("forgetful", actor="t")
    engine.enable("forgetful", actor="t")
    runtime = runtime_for()
    package = engine.get("forgetful")
    with pytest.raises(MediationError) as caught:
        runtime.write_memory(package, "big.txt", "x" * 65)
    assert caught.value.code == "MEMORY_BOUND"
    runtime.write_memory(package, "a.txt", "12345678")
    with pytest.raises(MediationError) as caught:
        runtime.write_memory(package, "b.txt", "12345678")
    assert caught.value.code == "MEMORY_BOUND"


def test_memory_none_retention_writes_nothing():
    engine = AgentCreationEngine()
    engine.create_from_template(
        "research", "volatile", created_by="t",
        overrides={"memory_policy": {"retention": "none"}})
    engine.validate("volatile", actor="t")
    engine.benchmark("volatile", actor="t")
    engine.enable("volatile", actor="t")
    runtime = runtime_for(fabric=FakeFabric())
    report = runtime.run(engine.get("volatile"), "hello", actor="tester")
    assert report["success"] and report["memory_key"] == ""


def test_memory_tools_served_by_mediation():
    engine = enabled_engine(name="memtools", template="research")
    runtime = runtime_for()
    package = engine.get("memtools")
    written = runtime.execute_tool(package, "memory_write", run_id="r1",
                                   key="k1", value="v1")
    assert written["success"] and written["gate"] == "not-required"
    read = runtime.execute_tool(package, "memory_read", run_id="r1",
                                key="k1")
    assert read["success"] and read["output"] == "v1"
    assert runtime.read_memory(package, "k1") == "v1"


# -- tool allowlist, gate, budget -----------------------------------------


def test_tool_allowlist_enforced():
    engine = enabled_engine()  # research: no terminal, no writes
    tools = FakeToolRuntime()
    runtime = runtime_for(fabric=FakeFabric(), tool_runtime=tools,
                          policy_gate=FakeGate("ALLOW"))
    package = engine.get("mediated-one")
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(package, "terminal", run_id="r1")
    assert caught.value.code == "TOOL_DENIED"
    assert tools.calls == []


def test_tool_budget_enforced():
    engine = AgentCreationEngine()
    engine.create_from_template(
        "research", "thrifty", created_by="t",
        overrides={"resource_limits": {"max_tool_calls_per_run": 1}})
    grant_all(engine, "thrifty", approver="t")
    engine.validate("thrifty", actor="t")
    engine.benchmark("thrifty", actor="t")
    engine.enable("thrifty", actor="t")
    tools = FakeToolRuntime()
    runtime = runtime_for(fabric=FakeFabric(), tool_runtime=tools,
                          policy_gate=FakeGate("ALLOW"))
    package = engine.get("thrifty")
    first = runtime.execute_tool(package, "memory_read", run_id="r1",
                                 key="k")
    assert first["success"]
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(package, "memory_read", run_id="r1", key="k")
    assert caught.value.code == "TOOL_BUDGET"


def test_mutating_tools_need_the_gate():
    engine = enabled_engine(name="coder-gated", template="coding")
    tools = FakeToolRuntime()
    gate = FakeGate("DENY")
    runtime = runtime_for(fabric=FakeFabric(), tool_runtime=tools,
                          policy_gate=gate)
    package = engine.get("coder-gated")
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(package, "write_file", run_id="r1",
                             path="src/x.md", content="x")
    assert caught.value.code == "GATE_DENIED"
    assert tools.calls == []
    assert gate.calls and gate.calls[0][1]["operation"] == "write_file"


def test_tools_need_a_runtime():
    engine = enabled_engine(name="norun-one", template="research")
    runtime = runtime_for(fabric=FakeFabric(), policy_gate=FakeGate())
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(engine.get("norun-one"), "read_file",
                             run_id="r1", path="app.py")
    assert caught.value.code == "NO_TOOL_RUNTIME"


def test_grant_shaped_tools_are_refused():
    engine = enabled_engine(name="ambitious", template="research")
    runtime = runtime_for(fabric=FakeFabric(), tool_runtime=FakeToolRuntime())
    package = engine.get("ambitious")
    for tool in ("grant_admin", "approve_request", "escalate_privs"):
        with pytest.raises(MediationError) as caught:
            runtime.execute_tool(package, tool, run_id="r1")
        assert caught.value.code == "TOOL_DENIED"
        assert "never grant permissions" in str(caught.value)


# -- real Tool Runtime, PolicyGate, checkpoints ---------------------------


def _real_runtime(tmp_path: Path, mode: str = "assisted"):
    from forge.runtime.defaults import create_default_runtime
    from forge.security.permissions import OperationMode, PermissionManager
    from forge.security.policy_gate import PolicyGate
    from forge.tools.checkpoint import CheckpointManager

    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    permissions = PermissionManager(mode=OperationMode(mode))
    return (GatedAgentRuntime(
        fabric=FakeFabric(), policy_gate=PolicyGate(permissions),
        tool_runtime=create_default_runtime(permissions, str(root)),
        memory_root=str(tmp_path / "mem"), project_root=str(root),
        checkpoint_manager=CheckpointManager(str(root))), root)


def test_execute_tool_runs_real_read(tmp_path: Path):
    engine = enabled_engine(name="reader", template="research")
    runtime, root = _real_runtime(tmp_path)
    (root / "app.py").write_text("def health(): return True\n")
    result = runtime.execute_tool(engine.get("reader"), "read_file",
                                  run_id="r1", path="app.py")
    assert result["success"] and "health" in result["output"]
    assert result["gate"] == "not-required"


def test_real_gate_denies_unapproved_write(tmp_path: Path):
    engine = enabled_engine(name="writer", template="coding")
    runtime, root = _real_runtime(tmp_path)  # assisted: writes need approval
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(engine.get("writer"), "write_file",
                             run_id="r1", path="src/note.md", content="hi")
    assert caught.value.code == "GATE_DENIED"
    assert not (root / "src" / "note.md").exists()


def test_approved_write_captures_checkpoint(tmp_path: Path):
    engine = enabled_engine(name="writer-ok", template="coding")
    runtime, root = _real_runtime(tmp_path)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_note.py").write_text(
        "def test_note():\n    assert True\n")
    report = runtime.run(
        engine.get("writer-ok"), "write a note", actor="tester",
        tool_calls=[{"tool": "write_file",
                     "args": {"path": "src/note.md", "content": "hi"}}],
        approved=True)
    assert report["success"] is True
    assert report["checkpoint_id"]  # snapshot taken before the write
    assert (root / "src" / "note.md").read_text() == "hi"


def test_run_rolls_back_on_tool_failure(tmp_path: Path):
    engine = enabled_engine(name="clumsy", template="coding")
    runtime, root = _real_runtime(tmp_path)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "keep.py").write_text("original\n")
    with pytest.raises(MediationError) as caught:
        runtime.run(
            engine.get("clumsy"), "write then fail", actor="tester",
            tool_calls=[
                {"tool": "write_file",
                 "args": {"path": "src/keep.py", "content": "changed"}},
                {"tool": "read_file",
                 "args": {"path": "does-not-exist.py"}}],
            approved=True)
    assert caught.value.code == "TOOL_FAILED"
    assert (root / "src" / "keep.py").read_text() == "original\n"


def test_run_rolls_back_on_verification_failure(tmp_path: Path):
    engine = enabled_engine(name="leaky-run", template="research")
    runtime, root = _real_runtime(tmp_path)
    runtime.fabric = FakeFabric(
        text='here is the key: api_key = "abcdefgh1234"')
    (root / "keep.py").write_text("original\n")
    with pytest.raises(MediationError) as caught:
        runtime.run(
            engine.get("leaky-run"), "show me secrets", actor="tester",
            tool_calls=[{"tool": "read_file",
                         "args": {"path": "keep.py"}}])
    assert caught.value.code == "VERIFICATION_FAILED"


# -- verification gates ---------------------------------------------------


def test_forbidden_output_fails_verification():
    engine = enabled_engine(name="leaky", template="research")
    fabric = FakeFabric(text='here is the key: api_key = "abcdefgh1234"')
    runtime = runtime_for(fabric=fabric)
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("leaky"), "show me secrets", actor="tester")
    assert caught.value.code == "VERIFICATION_FAILED"


def test_dangerous_output_fails_review():
    engine = enabled_engine(name="risky", template="research")
    fabric = FakeFabric(text="just run eval(user_input) to fix it")
    runtime = runtime_for(fabric=fabric)
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("risky"), "fix it", actor="tester")
    assert caught.value.code == "VERIFICATION_FAILED"


def test_require_tests_needs_a_command():
    engine = enabled_engine(name="needs-tests", template="coding")
    runtime = runtime_for(fabric=FakeFabric())
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("needs-tests"), "implement it",
                    actor="tester")
    assert caught.value.code == "VERIFICATION_FAILED"
    assert "TESTS_NOT_EXECUTED" in str(caught.value)


def test_require_tests_runs_the_command():
    engine = enabled_engine(name="tested-ok", template="coding")
    runtime = runtime_for(fabric=FakeFabric(),
                          tool_runtime=FakeToolRuntime())
    report = runtime.run(engine.get("tested-ok"), "implement it",
                         actor="tester")
    assert report["success"]
    assert report["verification"]["gates"]["tests"]["passed"] is True


# -- resource limits ------------------------------------------------------


def test_hourly_run_limit_enforced():
    engine = AgentCreationEngine()
    engine.create_from_template(
        "research", "limited", created_by="t",
        overrides={"resource_limits": {"max_runs_per_hour": 1}})
    engine.validate("limited", actor="t")
    engine.benchmark("limited", actor="t")
    engine.enable("limited", actor="t")
    runtime = runtime_for(fabric=FakeFabric())
    package = engine.get("limited")
    assert runtime.run(package, "one", actor="t")["success"]
    # Second run in the same hour is refused with no model call.
    with pytest.raises(MediationError) as caught:
        runtime.run(package, "two", actor="t")
    assert caught.value.code == "RATE_LIMITED"


# -- self-grant refusal at runtime ----------------------------------------


def test_runtime_refuses_self_approval():
    engine = enabled_engine()
    runtime = runtime_for(fabric=FakeFabric())
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("mediated-one"), "do it", actor="tester",
                    approver="agent:mediated-one")
    assert caught.value.code == "SELF_GRANT"


def test_approval_store_bars_agent_self_approval():
    from forge.security.approvals import ApprovalRequest, ApprovalStore

    store = ApprovalStore()
    request = store.submit(ApprovalRequest(
        agent="test-coder", resource=Resource.AGENT, operation="execute",
        scopes=("test-coder",), task_id="t1"))
    with pytest.raises(ValueError) as exc:
        store.decide(request.id, True, "test-coder")
    assert "cannot approve its own request" in str(exc.value)


# -- checkpoints ----------------------------------------------------------


def test_begin_mutation_checkpoints_and_rolls_back(tmp_path: Path):
    from forge.tools.checkpoint import CheckpointManager

    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("v1 = 1\n")
    engine = enabled_engine()
    runtime = runtime_for(
        fabric=FakeFabric(),
        checkpoint_manager=CheckpointManager(str(root)))
    package = engine.get("mediated-one")
    checkpoint = runtime.begin_mutation(package, declared=["app.py"])
    assert checkpoint is not None
    (root / "app.py").write_text("v2 = 2\n")
    assert runtime.rollback(checkpoint, ["app.py"]) is True
    assert (root / "app.py").read_text() == "v1 = 1\n"


def test_begin_mutation_refuses_non_enabled():
    runtime = runtime_for()
    with pytest.raises(MediationError) as caught:
        runtime.begin_mutation({"name": "x", "spec": {}, "state": "paused"})
    assert caught.value.code == "NOT_ENABLED"


# -- benchmark suite ------------------------------------------------------


def test_benchmark_scores_and_skips_honestly():
    engine = enabled_engine()
    package = engine.get("mediated-one")
    report = run_agent_benchmark(package)
    assert report["passed"] and report["score"] == 1.0
    assert report["executed"] == 10
    smoke = [check for check in report["checks"]
             if check["name"] == "model-smoke"][0]
    assert smoke["status"] == "skipped"
    fabric = FakeFabric(text="prefix %s suffix" % MARKER)
    report = run_agent_benchmark(package, fabric=fabric)
    smoke = [check for check in report["checks"]
             if check["name"] == "model-smoke"][0]
    assert smoke["status"] == "passed"


def test_benchmark_reports_all_checks():
    engine = enabled_engine()
    report = run_agent_benchmark(engine.get("mediated-one"))
    names = {check["name"] for check in report["checks"]}
    assert names == {"spec-valid", "tools-allowlisted",
                     "permissions-bounded", "memory-isolated",
                     "verification-declared", "limits-bounded",
                     "lifecycle-gate", "isolation-memory",
                     "permission-boundary", "grant-enforcement",
                     "model-smoke"}


def test_benchmark_fails_corrupt_spec():
    report = run_agent_benchmark({"name": "bad", "spec": {"name": "bad"},
                                  "state": "validated"})
    assert report["passed"] is False
    spec_check = [check for check in report["checks"]
                  if check["name"] == "spec-valid"][0]
    assert spec_check["status"] == "failed"


def test_benchmark_detects_corrupt_sections():
    engine = enabled_engine(name="rotten", template="research")
    package = engine.get("rotten")
    package.spec["tools"] = ["read_file", "teleport"]
    package.spec["permissions"] = [
        {"resource": "git", "operation": "stage", "scope": "x",
         "risk": "LOW", "reason": "smuggle"}]
    report = run_agent_benchmark({"name": package.name,
                                  "spec": package.spec,
                                  "state": "validated"})
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["tools-allowlisted"]["status"] == "failed"
    assert by_name["permissions-bounded"]["status"] == "failed"
    assert report["passed"] is False


# -- control plane integration --------------------------------------------


def test_plane_engine_lifecycle(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.engine_create(session, template="research",
                                      name="plane-scout")
        assert created["state"] == "created"
        validated = plane.engine_validate(session, "plane-scout")
        assert validated["valid"] is True
        tested = plane.engine_test(session, "plane-scout")
        assert tested["checks"], "benchmark must record checks"
        assert plane.engine_list(session)["agents"][0]["name"] == \
            "plane-scout"
        grant = plane.engine_grant(session, "plane-scout", 0)
        assert grant["grant"]["approver"] == "alice"
        # Grants reset the lifecycle: the agent must re-earn trust.
        assert plane.engine_get(session, "plane-scout")["state"] == \
            "created"
        release = plane.engine_version(session, "plane-scout",
                                       notes="first", kind="minor")
        assert release["release"]["version"] == "1.1.0"
        audited = plane.audit.query(resource="engine")
        assert {event.operation for event in audited} >= {
            "create", "validate", "test", "grant", "version"}


def test_plane_engine_update_resets(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.engine_create(session, template="coding",
                            name="plane-updated")
        plane.engine_validate(session, "plane-updated")
        spec = plane.engine_get(session, "plane-updated")["spec"]
        spec["purpose"] = "An updated purpose."
        updated = plane.engine_update(session, "plane-updated", spec,
                                      reason="test")
        assert updated["version"] == "1.0.1"
        assert updated["state"] == "created"


def test_plane_sessions_are_isolated(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    client = make_client(plane)
    with client:
        payload_a, _t1, _h1 = login(client, actor="alice")
        payload_b, _t2, _h2 = login(client, actor="bob")
        session_a = plane.sessions.get(payload_a["session_id"])
        session_b = plane.sessions.get(payload_b["session_id"])
        plane.engine_create(session_a, template="coding",
                            name="sess-agent")
        assert plane.engine_list(session_a)["agents"]
        assert plane.engine_list(session_b)["agents"] == []
        with pytest.raises(Exception):
            plane.engine_get(session_b, "sess-agent")


def test_plane_self_grant_refused(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.engine_create(session, template="research",
                            name="plane-ego")
        with pytest.raises(Exception, match="cannot grant themselves"):
            plane.engine_grant(session, "plane-ego", 0,
                               approver="agent:plane-ego")


def test_plane_engine_run_requires_enabled(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.engine_create(session, template="research",
                            name="plane-lazy")
        with pytest.raises(Exception, match="only enabled agents"):
            plane.engine_run(session, "plane-lazy", "do work")


def test_plane_engine_run_allowed_flow(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.engine_create(
            session, template="coding", name="plane-runner",
            overrides={"model_requirements": {"min_context_window": 1024}})
        plane.engine_validate(session, "plane-runner")
        tested = plane.engine_test(session, "plane-runner")
        assert tested["passed"]  # smoke failing still clears 0.8
        plane.engine_enable(session, "plane-runner")
        result = plane.engine_run(session, "plane-runner", "summarize")
        assert result["allowed"] is True
        assert result["run"]["success"] is True
        assert result["run"]["evidence"]["model"]["model"] == "m/a34"


def test_plane_engine_run_with_tools(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.engine_create(
            session, template="coding", name="plane-reader",
            overrides={"model_requirements": {"min_context_window": 1024}})
        for _index in range(len(plane.engine_get(
                session, "plane-reader")["spec"]["permissions"])):
            plane.engine_grant(session, "plane-reader", _index)
        plane.engine_validate(session, "plane-reader")
        assert plane.engine_test(session, "plane-reader")["passed"]
        plane.engine_enable(session, "plane-reader")
        result = plane.engine_run(
            session, "plane-reader", "read the app",
            tool_calls=[{"tool": "read_file", "args": {"path": "app.py"}}])
        assert result["allowed"] is True
        calls = result["run"]["tools"]
        assert calls[0]["tool"] == "read_file"
        assert "health" in calls[0]["output"]


def test_plane_engine_run_denied_by_policy(tmp_path: Path):
    denied = PermissionPolicy(rules=[
        PermissionRule(id="deny", resource=Resource.AGENT,
                       operation="execute", scope="", effect="DENY"),
    ])
    plane = make_plane(tmp_path, start=True, policy=denied)
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.engine_create(session, template="coding",
                            name="plane-blocked")
        plane.engine_validate(session, "plane-blocked")
        tested = plane.engine_test(session, "plane-blocked")
        assert tested["passed"]  # smoke failing still clears 0.8
        plane.engine_enable(session, "plane-blocked")
        result = plane.engine_run(session, "plane-blocked", "summarize")
        assert result["allowed"] is False
        assert result["run"] is None


def test_plane_engine_run_files_approval_when_gated(tmp_path: Path):
    gated = PermissionPolicy(rules=[
        PermissionRule(id="gate", resource=Resource.AGENT,
                       operation="execute", scope="",
                       effect="REQUIRE_APPROVAL"),
    ])
    plane = make_plane(tmp_path, start=True, policy=gated)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.engine_create(session, template="coding", name="plane-gated")
        plane.engine_validate(session, "plane-gated")
        plane.engine_test(session, "plane-gated")
        plane.engine_enable(session, "plane-gated")
        result = plane.engine_run(session, "plane-gated", "do work")
        assert result["allowed"] is False
        assert result["approval_required"] is True
        assert result["approval_request_id"]
        assert result["run"] is None


# -- API integration ------------------------------------------------------


def test_api_engine_flow(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/engine/templates").status_code == 401
        _payload, _token, headers = login(client)
        response = client.get("/api/v1/engine/templates", headers=headers)
        assert response.status_code == 200
        assert len(response.json()["templates"]) == 6
        response = client.post(
            "/api/v1/engine/agents", headers=headers,
            json={"template": "documentation", "name": "api-writer"})
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "created"
        response = client.get("/api/v1/engine/agents", headers=headers)
        assert response.json()["agents"][0]["name"] == "api-writer"
        response = client.post("/api/v1/engine/agents/api-writer/validate",
                               headers=headers)
        assert response.json()["valid"] is True
        response = client.post("/api/v1/engine/agents/api-writer/grant",
                               headers=headers, json={"index": 0})
        assert response.status_code == 200
        response = client.post("/api/v1/engine/agents/api-writer/version",
                               headers=headers,
                               json={"notes": "docs", "kind": "patch"})
        # Grant bumped 1.0.0 -> 1.0.1; release bumps to 1.0.2.
        assert response.json()["release"]["version"] == "1.0.2"
        response = client.post("/api/v1/engine/agents/api-writer/enable",
                               headers=headers)
        assert response.status_code == 400  # created, not tested


def test_api_engine_update_and_run(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _payload, _token, headers = login(client)
        response = client.post(
            "/api/v1/engine/agents", headers=headers,
            json={"template": "coding", "name": "api-runner",
                  "overrides": {"model_requirements":
                                {"min_context_window": 1024}}})
        assert response.status_code == 200
        spec = response.json()["spec"]
        spec["purpose"] = "Updated via API."
        response = client.patch(
            "/api/v1/engine/agents/api-runner", headers=headers,
            json={"spec": spec, "reason": "test"})
        assert response.status_code == 200, response.text
        assert response.json()["version"] == "1.0.1"
        for _index in range(len(spec["permissions"])):
            granted = client.post(
                "/api/v1/engine/agents/api-runner/grant", headers=headers,
                json={"index": _index})
            assert granted.status_code == 200
        client.post("/api/v1/engine/agents/api-runner/validate",
                    headers=headers)
        tested = client.post("/api/v1/engine/agents/api-runner/test",
                             headers=headers)
        assert tested.json()["passed"] is True
        enabled = client.post("/api/v1/engine/agents/api-runner/enable",
                              headers=headers)
        assert enabled.status_code == 200
        result = client.post(
            "/api/v1/engine/agents/api-runner/run", headers=headers,
            json={"requirement": "read the app",
                  "tool_calls": [{"tool": "read_file",
                                  "args": {"path": "app.py"}}]})
        assert result.status_code == 200, result.text
        assert result.json()["run"]["success"] is True


def test_api_engine_rejects_invalid(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    client = make_client(plane)
    with client:
        _payload, _token, headers = login(client)
        response = client.post(
            "/api/v1/engine/agents", headers=headers,
            json={"template": "nope", "name": "bad"})
        assert response.status_code == 400
        response = client.get("/api/v1/engine/agents/ghost",
                              headers=headers)
        assert response.status_code == 400


# -- CLI integration ------------------------------------------------------


def run_cli(argv: list[str], tmp_path: Path, monkeypatch,
            capsys) -> tuple[int, str]:
    monkeypatch.chdir(tmp_path)
    code = 0
    with patch.object(sys, "argv", ["forge"] + argv):
        try:
            cli_main()
        except SystemExit as exit:
            code = int(exit.code or 0)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_cli_agents_lifecycle(tmp_path: Path, monkeypatch, capsys):
    code, out = run_cli(["agents", "templates"], tmp_path, monkeypatch,
                        capsys)
    assert code == 0 and "coding" in out
    code, out = run_cli(
        ["agents", "create", "--template", "research", "--name",
         "cli-scout"], tmp_path, monkeypatch, capsys)
    assert code == 0 and "cli-scout" in out
    code, out = run_cli(["agents", "validate", "cli-scout"], tmp_path,
                        monkeypatch, capsys)
    assert code == 0
    code, out = run_cli(["agents", "test", "cli-scout"], tmp_path,
                        monkeypatch, capsys)
    assert code == 0 and "passed" in out
    code, _out = run_cli(["agents", "enable", "cli-scout"], tmp_path,
                         monkeypatch, capsys)
    assert code == 0
    code, out = run_cli(["agents"], tmp_path, monkeypatch, capsys)
    assert code == 0 and "cli-scout" in out and "enabled" in out
    code, _out = run_cli(["agents", "disable", "cli-scout"], tmp_path,
                         monkeypatch, capsys)
    assert code == 0
    code, out = run_cli(["agents", "enable", "cli-scout"], tmp_path,
                        monkeypatch, capsys)
    assert code == 0


def test_cli_agents_flags_after_subcommand(tmp_path: Path, monkeypatch,
                                           capsys):
    store = str(tmp_path / "custom.json")
    code, _out = run_cli(
        ["agents", "create", "--template", "coding", "--name", "flaggy",
         "--store", store, "--actor", "alice"], tmp_path, monkeypatch,
        capsys)
    assert code == 0
    assert (tmp_path / "custom.json").exists()
    code, out = run_cli(
        ["agents", "show", "flaggy", "--store", store], tmp_path,
        monkeypatch, capsys)
    assert code == 0 and "flaggy" in out
    code, out = run_cli(["agents", "templates", "--json"], tmp_path,
                        monkeypatch, capsys)
    assert code == 0
    assert len(json.loads(out)["templates"]) == 6


def test_cli_agents_rejects_bad_input(tmp_path: Path, monkeypatch,
                                      capsys):
    code, _out = run_cli(
        ["agents", "create", "--template", "nope", "--name", "x"],
        tmp_path, monkeypatch, capsys)
    assert code != 0
    code, _out = run_cli(["agents", "enable", "ghost"], tmp_path,
                         monkeypatch, capsys)
    assert code != 0


# -- desktop backend integration ------------------------------------------


def test_desktop_backend_agent_manager(tmp_path: Path):
    from forge.desktop_app.backend import DesktopBackend

    project = tmp_path / "demo"
    project.mkdir()
    backend = DesktopBackend(actor="tester")
    backend.start({"demo": str(project)})
    try:
        assert len(backend.agents_templates()) == 6
        created = backend.agents_create("demo", "coding", "desk-coder")
        assert created["state"] == "created"
        assert backend.agents_validate("demo", "desk-coder")["valid"]
        tested = backend.agents_test("demo", "desk-coder")
        assert tested["passed"]
        assert backend.agents_enable("demo", "desk-coder")["state"] == \
            "enabled"
        assert backend.agents_disable("demo", "desk-coder")["state"] == \
            "disabled"
        listed = backend.agents_list("demo")
        assert [entry["name"] for entry in listed] == ["desk-coder"]
        shown = backend.agents_show("demo", "desk-coder")
        assert shown["manifest"]["state"] == "disabled"
        release = backend.agents_version("demo", "desk-coder",
                                         kind="minor", notes="ui")
        assert release["release"]["version"] == "1.1.0"
    finally:
        backend.stop()


def test_desktop_backend_grant_revoke(tmp_path: Path):
    from forge.desktop_app.backend import DesktopBackend

    project = tmp_path / "demo"
    project.mkdir()
    backend = DesktopBackend(actor="tester")
    backend.start({"demo": str(project)})
    try:
        backend.agents_create("demo", "research", "desk-scout")
        granted = backend.agents_grant("demo", "desk-scout", 0)
        assert granted["grant"]["approver"] == "tester"
        revoked = backend.agents_revoke("demo", "desk-scout", 0)
        assert revoked["revoked"]["approver"] == "tester"
    finally:
        backend.stop()


def test_desktop_backend_unknown_project(tmp_path: Path):
    from forge.desktop_app.backend import BackendError, DesktopBackend

    backend = DesktopBackend(actor="tester")
    with pytest.raises(BackendError):
        backend.agents_list("ghost")


def test_desktop_agent_manager_window(monkeypatch):
    import types

    from forge.desktop_app.backend import DesktopBackend

    tk = types.ModuleType("tkinter")
    ttk = types.ModuleType("tkinter.ttk")
    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askdirectory = lambda **k: ""
    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.showerror = lambda *a, **k: None
    messagebox.showinfo = lambda *a, **k: None
    messagebox.askyesno = lambda *a, **k: False

    class _Widget:
        def __init__(self, *a, **k):
            self._config = dict(k)

        def __getattr__(self, item):
            def _call(*a, **k):
                if item == "curselection":
                    return ()
                if item == "winfo_children":
                    return []
                if item == "cget" and a:
                    return self._config.get(a[0], "")
                return None
            return _call

        def __setitem__(self, key, value):
            self._config[key] = value

        def __getitem__(self, key):
            return self._config.get(key)

    class _Var:
        def __init__(self, value=""):
            self._value = value

        def get(self):
            return self._value

        def set(self, value):
            self._value = value

    tk.Tk = _Widget
    tk.Toplevel = _Widget
    tk.Menu = _Widget
    tk.Text = _Widget
    tk.Listbox = _Widget
    tk.Label = _Widget
    tk.StringVar = _Var
    tk.TclError = type("TclError", (Exception,), {})

    def _const(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return name.lower()

    tk.__getattr__ = _const  # type: ignore[attr-defined]
    for widget in ("Frame", "Label", "Button", "Combobox", "Panedwindow",
                   "PanedWindow", "Notebook", "Scrollbar", "LabelFrame",
                   "Entry"):
        setattr(ttk, widget, _Widget)
    tk.ttk = ttk  # type: ignore[attr-defined]
    tk.filedialog = filedialog  # type: ignore[attr-defined]
    tk.messagebox = messagebox  # type: ignore[attr-defined]
    sys.modules.pop("forge.desktop_app.app", None)
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", filedialog)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", messagebox)
    import forge.desktop_app.app as app_module

    window = app_module.AgentManagerWindow(_Widget(), DesktopBackend(),
                                           "demo")
    assert window.project_id == "demo"
    assert "validate" in window.OPERATIONS


# -- decision vocabulary --------------------------------------------------


def test_policy_decision_values_stable():
    assert PolicyDecision.ALLOW.value == "ALLOW"
    assert PolicyDecision.DENY.value == "DENY"
    assert PolicyDecision.REQUIRE_APPROVAL.value == "REQUIRE_APPROVAL"


# -- grant enforcement -----------------------------------------------------

def test_ungranted_power_tool_refused():
    engine = AgentCreationEngine()
    engine.create_from_template("research", "bare", created_by="t")
    engine.validate("bare", actor="t")
    engine.benchmark("bare", actor="t")
    engine.enable("bare", actor="t")
    runtime = runtime_for(fabric=FakeFabric(),
                          tool_runtime=FakeToolRuntime())
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(engine.get("bare"), "read_file", run_id="r1",
                             path="app.py")
    assert caught.value.code == "TOOL_DENIED"
    assert "no grant" in str(caught.value)


def test_grant_scope_must_cover_filesystem_path():
    engine = enabled_engine(name="scoped", template="coding")
    runtime = runtime_for(fabric=FakeFabric(),
                          tool_runtime=FakeToolRuntime(),
                          policy_gate=FakeGate("ALLOW"))
    package = engine.get("scoped")
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(package, "write_file", run_id="r1",
                             path="elsewhere/x.md", content="x")
    assert caught.value.code == "TOOL_DENIED"
    result = runtime.execute_tool(package, "write_file", run_id="r1",
                                  path="src/x.md", content="x")
    assert result["success"]


def _terminal_spec(name: str) -> dict:
    return {
        "name": name,
        "purpose": "Run pinned commands.",
        "capabilities": ["coding"],
        "tools": ["terminal"],
        "permissions": [
            {"resource": "terminal", "operation": "execute",
             "scope": "pytest", "risk": "MEDIUM", "reason": "Tests."},
        ],
        "model_requirements": {"capabilities": ["coding"]},
        "memory_policy": {"retention": "session"},
        "verification_requirements": {},
        "resource_limits": {},
    }


def _terminal_agent(name: str = "term") -> AgentCreationEngine:
    engine = AgentCreationEngine()
    engine.create_from_spec(_terminal_spec(name), created_by="t")
    engine.grant_permission(name, 0, approver="t")
    engine.validate(name, actor="t")
    engine.benchmark(name, actor="t")
    engine.enable(name, actor="t")
    return engine


def test_terminal_grant_pins_exact_executable():
    engine = _terminal_agent()
    tools = FakeToolRuntime()
    runtime = runtime_for(fabric=FakeFabric(), tool_runtime=tools,
                          policy_gate=FakeGate("ALLOW"))
    package = engine.get("term")
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(package, "terminal", run_id="r1",
                             command=["/bin/sh", "-c", "id"])
    assert caught.value.code == "TOOL_DENIED"
    result = runtime.execute_tool(package, "terminal", run_id="r1",
                                  command=["pytest", "-q"])
    assert result["success"]
    assert tools.calls and tools.calls[0][0] == "terminal"


def test_token_chain_shared_between_gate_and_runtime():
    engine = _terminal_agent(name="chained")
    tools = FakeToolRuntime()
    gate = FakeGate("ALLOW")
    runtime = runtime_for(fabric=FakeFabric(), tool_runtime=tools,
                          policy_gate=gate)
    runtime.execute_tool(engine.get("chained"), "terminal", run_id="r1",
                         command=["pytest", "-q"])
    gate_kwargs = gate.calls[0][1]
    tool_kwargs = dict(tools.calls[0][1])
    assert gate_kwargs["operation"] == "run_command"
    assert gate_kwargs["request_id"] == tool_kwargs["request_id"]
    assert gate_kwargs["request_id"].startswith("r1:")
    assert gate_kwargs["task_id"] == tool_kwargs["task_id"] == "r1"
    assert gate_kwargs["risk"] == tool_kwargs["risk"] == "MEDIUM"


def test_run_needs_named_non_agent_actor():
    engine = enabled_engine()
    runtime = runtime_for(fabric=FakeFabric())
    package = engine.get("mediated-one")
    with pytest.raises(MediationError) as caught:
        runtime.run(package, "hi")
    assert caught.value.code == "BAD_ACTOR"
    with pytest.raises(MediationError) as caught:
        runtime.run(package, "hi", actor="agent:mediated-one")
    assert caught.value.code == "SELF_RUN"


def test_security_gate_never_echoes_secrets():
    engine = enabled_engine(name="leaky-two", template="research")
    runtime = runtime_for(
        fabric=FakeFabric(text='key: api_key = "abcdefgh1234"'))
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("leaky-two"), "show", actor="tester")
    assert caught.value.code == "VERIFICATION_FAILED"
    assert "abcdefgh" not in str(caught.value)
    assert "secret-pattern#" in str(caught.value)


def test_run_schema_has_no_approval_or_command_fields():
    from forge.api.schemas import EngineRunRequest

    body = EngineRunRequest(requirement="x", approved=True,
                            test_command="id")
    assert not hasattr(body, "approved")
    assert not hasattr(body, "test_command")


def test_api_cannot_self_approve_writes(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _payload, _token, headers = login(client)
        created = client.post(
            "/api/v1/engine/agents", headers=headers,
            json={"template": "coding", "name": "api-sneaky",
                  "overrides": {"model_requirements":
                                {"min_context_window": 1024}}})
        assert created.status_code == 200
        perms = created.json()["spec"]["permissions"]
        for _index in range(len(perms)):
            granted = client.post(
                "/api/v1/engine/agents/api-sneaky/grant", headers=headers,
                json={"index": _index})
            assert granted.status_code == 200
        client.post("/api/v1/engine/agents/api-sneaky/validate",
                    headers=headers)
        tested = client.post("/api/v1/engine/agents/api-sneaky/test",
                             headers=headers)
        assert tested.json()["passed"] is True
        enabled = client.post("/api/v1/engine/agents/api-sneaky/enable",
                              headers=headers)
        assert enabled.status_code == 200
        result = client.post(
            "/api/v1/engine/agents/api-sneaky/run", headers=headers,
            json={"requirement": "write",
                  "approved": True,
                  "tool_calls": [{"tool": "write_file",
                                  "args": {"path": "src/evil.py",
                                           "content": "x"}}]})
        assert result.status_code == 400
        assert "GATE_DENIED" in result.text
        assert not (Path(plane.projects["demo"].root)
                    / "src" / "evil.py").exists()


def test_api_rejects_oversized_spec(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    client = make_client(plane)
    with client:
        _payload, _token, headers = login(client)
        response = client.post(
            "/api/v1/engine/agents", headers=headers,
            json={"spec": {"name": "big", "purpose": "x" * 70000}})
        assert response.status_code == 400
        assert "exceeds" in response.text


def test_cli_grant_expect_pin(tmp_path: Path, monkeypatch, capsys):
    code, _out = run_cli(
        ["agents", "create", "--template", "research",
         "--name", "cli-pin"], tmp_path, monkeypatch, capsys)
    assert code == 0
    code, out = run_cli(["agents", "show", "cli-pin"], tmp_path,
                        monkeypatch, capsys)
    assert code == 0 and "permission[0]" in out
    code, _out = run_cli(
        ["agents", "grant", "cli-pin", "0", "--expect-json", "{}"],
        tmp_path, monkeypatch, capsys)
    assert code != 0
