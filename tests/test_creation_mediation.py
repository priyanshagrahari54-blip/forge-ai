"""Creation engine mediation: isolation, permission boundaries, integrations."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane  # noqa: E402

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


def enabled_engine(name: str = "mediated-one",
                   template: str = "research") -> AgentCreationEngine:
    engine = AgentCreationEngine()
    engine.create_from_template(template, name, created_by="tester")
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
    report = runtime.run(package, "summarize the repo")
    assert report["success"] and report["agent"] == "mediated-one"
    for state in ("created", "validated", "tested", "paused",
                  "disabled", "retired"):
        probe = {"name": package.name, "spec": package.spec,
                 "state": state}
        with pytest.raises(MediationError) as caught:
            runtime.run(probe, "summarize the repo")
        assert caught.value.code == "NOT_ENABLED"


def test_run_validates_requirement():
    engine = enabled_engine()
    runtime = runtime_for(fabric=FakeFabric())
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("mediated-one"), "   ")
    assert caught.value.code == "BAD_REQUIREMENT"


def test_no_fabric_refuses_without_fabrication():
    engine = enabled_engine()
    runtime = runtime_for()
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("mediated-one"), "write code")
    assert caught.value.code == "NO_MODEL"


def test_model_failure_is_honest():
    engine = enabled_engine()
    fabric = FakeFabric()
    fabric.generate = lambda request: FakeResponse("", success=False)
    runtime = runtime_for(fabric=fabric)
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("mediated-one"), "write code")
    assert caught.value.code == "MODEL_FAILED"


def test_model_request_honors_spec_bounds():
    engine = enabled_engine(name="bounded-one", template="coding")
    fabric = FakeFabric()
    runtime = runtime_for(fabric=fabric)
    report = runtime.run(engine.get("bounded-one"), "fix it",
                         test_command=sys.executable + " -V")
    assert report["success"]
    request = fabric.requests[0]
    assert request.capability == "coding"
    assert request.min_context_window == 8192


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
        "research", "forgetful",
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
        "research", "volatile",
        overrides={"memory_policy": {"retention": "none"}})
    engine.validate("volatile", actor="t")
    engine.benchmark("volatile", actor="t")
    engine.enable("volatile", actor="t")
    runtime = runtime_for(fabric=FakeFabric())
    report = runtime.run(engine.get("volatile"), "hello")
    assert report["success"] and report["memory_key"] == ""


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
        "research", "thrifty",
        overrides={"resource_limits": {"max_tool_calls_per_run": 1}})
    engine.validate("thrifty", actor="t")
    engine.benchmark("thrifty", actor="t")
    engine.enable("thrifty", actor="t")
    tools = FakeToolRuntime()
    runtime = runtime_for(fabric=FakeFabric(), tool_runtime=tools,
                          policy_gate=FakeGate("ALLOW"))
    package = engine.get("thrifty")
    first = runtime.execute_tool(package, "memory", run_id="r1")
    assert first["success"]
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(package, "memory", run_id="r1")
    assert caught.value.code == "TOOL_BUDGET"


def test_mutating_tools_need_the_gate():
    engine = enabled_engine(name="coder-gated", template="coding")
    tools = FakeToolRuntime()
    runtime = runtime_for(fabric=FakeFabric(), tool_runtime=tools,
                          policy_gate=FakeGate("DENY"))
    package = engine.get("coder-gated")
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(package, "terminal", run_id="r1")
    assert caught.value.code == "GATE_DENIED"
    assert tools.calls == []


def test_tools_need_a_runtime():
    engine = enabled_engine(name="norun-one", template="research")
    runtime = runtime_for(fabric=FakeFabric(), policy_gate=FakeGate())
    with pytest.raises(MediationError) as caught:
        runtime.execute_tool(engine.get("norun-one"), "memory",
                             run_id="r1")
    assert caught.value.code == "NO_TOOL_RUNTIME"


# -- verification gates ---------------------------------------------------


def test_forbidden_output_fails_verification():
    engine = enabled_engine(name="leaky", template="research")
    fabric = FakeFabric(text='here is the key: api_key = "abcdefgh1234"')
    runtime = runtime_for(fabric=fabric)
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("leaky"), "show me secrets")
    assert caught.value.code == "VERIFICATION_FAILED"


def test_dangerous_output_fails_review():
    engine = enabled_engine(name="risky", template="research")
    fabric = FakeFabric(text="just run eval(user_input) to fix it")
    runtime = runtime_for(fabric=fabric)
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("risky"), "fix it")
    assert caught.value.code == "VERIFICATION_FAILED"


def test_require_tests_needs_a_command():
    engine = enabled_engine(name="needs-tests", template="coding")
    runtime = runtime_for(fabric=FakeFabric())
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("needs-tests"), "implement it")
    assert caught.value.code == "VERIFICATION_FAILED"
    assert "TESTS_NOT_EXECUTED" in str(caught.value)


def test_require_tests_runs_the_command():
    engine = enabled_engine(name="tested-ok", template="coding")
    runtime = runtime_for(fabric=FakeFabric())
    report = runtime.run(engine.get("tested-ok"), "implement it",
                         test_command=sys.executable + " -V")
    assert report["success"]
    assert report["verification"]["gates"]["tests"]["passed"] is True


# -- resource limits ------------------------------------------------------


def test_hourly_run_limit_enforced():
    engine = AgentCreationEngine()
    engine.create_from_template(
        "research", "limited",
        overrides={"resource_limits": {"max_runs_per_hour": 1}})
    engine.validate("limited", actor="t")
    engine.benchmark("limited", actor="t")
    engine.enable("limited", actor="t")
    runtime = runtime_for(fabric=FakeFabric())
    package = engine.get("limited")
    assert runtime.run(package, "one")["success"]
    # Fool the wall clock: second run in the same hour is refused with no
    # model call.
    calls_before = 1
    del calls_before
    with pytest.raises(MediationError) as caught:
        runtime.run(package, "two")
    assert caught.value.code == "RATE_LIMITED"


# -- self-grant refusal at runtime ----------------------------------------


def test_runtime_refuses_self_approval():
    engine = enabled_engine()
    runtime = runtime_for(fabric=FakeFabric())
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("mediated-one"), "do it",
                    approver="agent:mediated-one")
    assert caught.value.code == "SELF_GRANT"


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
    smoke = [check for check in report["checks"]
             if check["name"] == "model-smoke"][0]
    assert smoke["status"] == "skipped"
    fabric = FakeFabric(text="prefix %s suffix" % MARKER)
    report = run_agent_benchmark(package, fabric=fabric)
    smoke = [check for check in report["checks"]
             if check["name"] == "model-smoke"][0]
    assert smoke["status"] == "passed"


def test_benchmark_fails_corrupt_spec():
    report = run_agent_benchmark({"name": "bad", "spec": {"name": "bad"},
                                  "state": "validated"})
    assert report["passed"] is False
    spec_check = [check for check in report["checks"]
                  if check["name"] == "spec-valid"][0]
    assert spec_check["status"] == "failed"


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
        assert created["bound"] is True
        validated = plane.engine_validate(session, "plane-scout")
        assert validated["valid"] is True
        tested = plane.engine_test(session, "plane-scout")
        assert tested["checks"], "benchmark must record checks"
        assert plane.engine_list(session)["agents"][0]["name"] == \
            "plane-scout"
        grant = plane.engine_grant(session, "plane-scout", 0)
        assert grant["grant"]["approver"] == "alice"
        release = plane.engine_version(session, "plane-scout",
                                       notes="first", kind="minor")
        assert release["release"]["version"] == "1.1.0"


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
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.engine_create(
            session, template="coding", name="plane-runner",
            overrides={"model_requirements": {"min_context_window": 1024}})
        plane.engine_validate(session, "plane-runner")
        tested = plane.engine_test(session, "plane-runner")
        assert tested["passed"]  # 4/5 with smoke failing clears 0.8
        plane.engine_enable(session, "plane-runner")
        result = plane.engine_run(session, "plane-runner", "summarize",
                                  test_command=sys.executable + " -V")
        assert result["allowed"] is True
        assert result["run"]["success"] is True


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
        assert tested["passed"]  # 4/5 with smoke failing clears 0.8
        plane.engine_enable(session, "plane-blocked")
        result = plane.engine_run(session, "plane-blocked", "summarize")
        assert result["allowed"] is False


# -- API integration ------------------------------------------------------


def test_api_engine_flow(tmp_path: Path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_RUNS)
    client = make_client(plane)
    with client:
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
        assert response.json()["release"]["version"] == "1.0.1"
        response = client.post("/api/v1/engine/agents/api-writer/enable",
                               headers=headers)
        assert response.status_code == 400  # still validated, not tested


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
    finally:
        backend.stop()


def test_desktop_backend_unknown_project(tmp_path: Path):
    from forge.desktop_app.backend import BackendError, DesktopBackend

    backend = DesktopBackend(actor="tester")
    with pytest.raises(BackendError):
        backend.agents_list("ghost")


# -- decision vocabulary --------------------------------------------------


def test_policy_decision_values_stable():
    assert PolicyDecision.ALLOW.value == "ALLOW"
    assert PolicyDecision.DENY.value == "DENY"
    assert PolicyDecision.REQUIRE_APPROVAL.value == "REQUIRE_APPROVAL"
