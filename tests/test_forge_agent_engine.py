"""First-party Forge Agent Creation Engine.

Specs, templates, factory packages, lifecycle, versioning,
benchmarks, guarded execution, and the CLI / API / desktop surfaces.
Isolation and permission-boundary tests prove agents cannot escape
their namespace, tools, or allowlist — and can never self-grant.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (login, make_client, make_fabric, make_plane,  # noqa: E402
                         make_repo, ScriptedProvider)

from forge.agents.agent_benchmarks import (build_test_report,  # noqa: E402
                                           run_agent_benchmark,
                                           structural_checks)
from forge.agents.engine import (AgentCreationEngine, AgentEngineError,  # noqa: E402
                                 SelfGrantDenied)
from forge.agents.lifecycle import (LifecycleState, can_transition,  # noqa: E402
                                    check_transition, is_runnable)
from forge.agents.package import build_package  # noqa: E402
from forge.agents.spec import (AgentSpec, MemoryPolicy,  # noqa: E402
                               ModelRequirements, ResourceLimits,
                               VerificationRequirements)
from forge.agents.store import AgentStore  # noqa: E402
from forge.agents.templates import (TEMPLATE_NAMES, build_from_template,  # noqa: E402
                                    describe_all)
from forge.agents.versioning import bump, parse, validate  # noqa: E402
from forge.cli import main as forge_main  # noqa: E402
from forge.models.provider import ModelResult  # noqa: E402
from forge.security.policy import (PermissionPolicy, PermissionRule,  # noqa: E402
                                   Resource)


RUN_POLICY = PermissionPolicy(rules=[
    PermissionRule(id="a-run", resource=Resource.AGENT,
                   operation="execute", scope="", effect="ALLOW"),
    PermissionRule(id="fs-w", resource=Resource.FILESYSTEM,
                   operation="write", scope="**", effect="ALLOW"),
])

DENY_POLICY = PermissionPolicy(rules=[
    PermissionRule(id="a-deny", resource=Resource.AGENT,
                   operation="execute", scope="", effect="DENY"),
])


def run_cli(argv):
    with patch.object(sys, "argv", argv):
        forge_main()


def make_engine(tmp_path, **overrides):
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text("def health(): return True\n")
    kwargs = {"root": str(root), "fabric": make_fabric(
        ScriptedProvider()), "session_id": "test"}
    kwargs.update(overrides)
    return AgentCreationEngine(**kwargs)


def enable_researcher(engine, name="scout", actor="operator"):
    engine.create_from_template("research", name, created_by=actor,
                                bind=True)
    engine.validate(name, actor)
    engine.test(name, actor)
    return engine.enable(name, actor)


# -- agent specification -------------------------------------------------------

def test_spec_builds_from_all_fields():
    spec = AgentSpec(
        name="helper-one",
        purpose="Help with chores.",
        capabilities=("coding", "review"),
        tools=("read_file", "write_file"),
        permissions=("read_file", "write_file"),
        model_requirements=ModelRequirements(("coding",)),
        memory_policy=MemoryPolicy(),
        verification_requirements=VerificationRequirements(),
        resource_limits=ResourceLimits())
    assert spec.name == "helper-one"
    assert spec.capabilities == ("coding", "review")


def test_spec_rejects_bad_identity():
    with pytest.raises(ValueError):
        AgentSpec(name="Bad Name!", purpose="p",
                  capabilities=("coding",), tools=("read_file",),
                  permissions=("read_file",))
    with pytest.raises(ValueError):
        AgentSpec(name="good-name", purpose="",
                  capabilities=("coding",), tools=("read_file",),
                  permissions=("read_file",))
    with pytest.raises(ValueError):
        AgentSpec(name="good-name", purpose="p",
                  capabilities=(), tools=("read_file",),
                  permissions=("read_file",))


def test_spec_rejects_unknown_capability_tool_permission():
    with pytest.raises(ValueError):
        AgentSpec(name="agent-one", purpose="p",
                  capabilities=("mind-reading",),
                  tools=("read_file",), permissions=("read_file",))
    with pytest.raises(ValueError):
        AgentSpec(name="agent-one", purpose="p",
                  capabilities=("coding",), tools=("mind_control",),
                  permissions=("read_file",))
    with pytest.raises(ValueError):
        AgentSpec(name="agent-one", purpose="p",
                  capabilities=("coding",), tools=("read_file",),
                  permissions=("mind_control",))


def test_spec_rejects_blocked_operations():
    for blocked in ("delete_repository", "expose_secrets"):
        with pytest.raises(ValueError, match="Blocked operation"):
            AgentSpec(name="agent-one", purpose="p",
                      capabilities=("coding",),
                      tools=("read_file",),
                      permissions=("read_file", blocked))


def test_spec_rejects_bad_sub_policies():
    with pytest.raises(ValueError):
        ModelRequirements(())
    with pytest.raises(ValueError):
        ModelRequirements(("mind-reading",))
    with pytest.raises(ValueError):
        MemoryPolicy(max_entries=0)
    with pytest.raises(ValueError):
        VerificationRequirements(min_benchmark_pass_rate=2.0)
    with pytest.raises(ValueError):
        ResourceLimits(max_runs_per_hour=0)
    with pytest.raises(ValueError):
        ResourceLimits(max_concurrent=99)


def test_spec_round_trip():
    spec = build_from_template("coding", "coder-one")
    clone = AgentSpec.from_dict(spec.to_dict())
    assert clone.to_dict() == spec.to_dict()
    with pytest.raises(ValueError):
        AgentSpec.from_dict({"name": "x"})


# -- templates ------------------------------------------------------------------

def test_all_templates_validate():
    assert set(TEMPLATE_NAMES) == {
        "coding", "research", "security", "game-development",
        "os-development", "documentation"}
    for template in TEMPLATE_NAMES:
        spec = build_from_template(template, "agent-%s" % template)
        assert spec.name == "agent-%s" % template
        assert spec.purpose
        # Every template round-trips through full validation.
        AgentSpec.from_dict(spec.to_dict())
    with pytest.raises(ValueError):
        build_from_template("nope", "agent-x")
    assert len(describe_all()) == 6


def test_research_template_is_read_only():
    spec = build_from_template("research", "scout-one")
    assert "write_file" not in spec.permissions
    assert "write_file" not in spec.tools
    assert "terminal" not in spec.tools


# -- factory packages -------------------------------------------------------------

def test_factory_builds_structured_package():
    spec = build_from_template("coding", "builder-one")
    package = build_package(spec, created_by="operator", bind=True)
    assert package.version == "1.0.0"
    assert package.lifecycle == LifecycleState.CREATED.value
    assert package.real is True
    assert package.executor == "coder"
    assert package.version_history
    manifest = package.manifest()
    assert manifest["name"] == "builder-one"
    assert set(manifest["spec"]) >= {
        "name", "purpose", "capabilities", "tools", "permissions",
        "model_requirements", "memory_policy",
        "verification_requirements", "resource_limits"}
    wiring = manifest["wiring"]
    assert wiring["model_fabric"] is True
    assert wiring["policy_gate"] is True
    assert wiring["tool_runtime"] is True
    assert wiring["memory"] == "namespaced:builder-one"
    assert wiring["verification"] is True
    assert wiring["checkpoints"] is True


def test_package_real_flag_is_honest():
    spec = build_from_template("coding", "builder-two")
    plain = build_package(spec, created_by="operator", bind=False)
    assert plain.real is False
    assert "no executor" in plain.manifest()["note"]
    # A capability with no backing executor can never be real.
    browserish = AgentSpec(
        name="browser-one", purpose="Browse.",
        capabilities=("browser",), tools=("read_file",),
        permissions=("read_file",),
        model_requirements=ModelRequirements(("browser",)))
    package = build_package(browserish, created_by="operator",
                            bind=True)
    assert package.real is False
    assert package.executor == ""


# -- lifecycle --------------------------------------------------------------------

def test_lifecycle_transitions(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("research", "scout", created_by="op",
                                bind=True)
    assert engine.get("scout").lifecycle == "created"
    with pytest.raises(Exception):
        engine.enable("scout", "op")  # created -> enabled is illegal
    engine.validate("scout", "op")
    with pytest.raises(Exception):
        engine.enable("scout", "op")  # validated is not tested
    engine.test("scout", "op")
    assert engine.get("scout").lifecycle == "tested"
    engine.enable("scout", "op")
    engine.pause("scout", "op")
    engine.enable("scout", "op")
    engine.disable("scout", "op")
    engine.enable("scout", "op")
    engine.retire("scout", "op")
    assert engine.get("scout").lifecycle == "retired"
    with pytest.raises(Exception):
        engine.enable("scout", "op")  # retired is terminal
    with pytest.raises(Exception):
        check_transition("retired", "enabled")
    assert can_transition("enabled", "paused") is True
    assert can_transition("created", "enabled") is False
    assert is_runnable("enabled") is True
    assert is_runnable("paused") is False


def test_only_enabled_agents_run(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("research", "scout", created_by="op",
                                bind=True)
    with pytest.raises(AgentEngineError, match="only enabled"):
        engine.execute("scout", "count files")
    engine.validate("scout", "op")
    with pytest.raises(AgentEngineError, match="only enabled"):
        engine.execute("scout", "count files")
    engine.test("scout", "op")
    with pytest.raises(AgentEngineError, match="only enabled"):
        engine.execute("scout", "count files")


def test_unbound_agents_cannot_enable(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("research", "ghost", created_by="op",
                                bind=False)
    engine.validate("ghost", "op")
    engine.test("ghost", "op")
    with pytest.raises(AgentEngineError, match="no bound executor"):
        engine.enable("ghost", "op")


# -- versioning ---------------------------------------------------------------------

def test_versioning_helpers():
    assert parse("1.2.3") == (1, 2, 3)
    assert validate("1.2.3") == "1.2.3"
    assert bump("1.2.3") == "1.2.4"
    assert bump("1.2.3", "minor") == "1.3.0"
    assert bump("1.2.3", "major") == "2.0.0"
    with pytest.raises(ValueError):
        parse("1.2")
    with pytest.raises(ValueError):
        bump("1.2.3", "mega")


def test_update_bumps_version_and_requires_retest(tmp_path):
    engine = make_engine(tmp_path)
    enable_researcher(engine, "scout")
    assert engine.get("scout").version == "1.0.0"
    payload = engine.get("scout").spec.to_dict()
    payload["purpose"] = "A refined purpose."
    updated = engine.update("scout", payload, changed_by="op")
    assert updated.version == "1.0.1"
    assert updated.lifecycle == "validated"  # testing invalidated
    assert updated.test_report == {}
    history = engine.versions("scout")["history"]
    assert [entry["version"] for entry in history] == ["1.0.0",
                                                      "1.0.1"]
    with pytest.raises(AgentEngineError):
        engine.update("nope", payload, changed_by="op")


# -- benchmarks ----------------------------------------------------------------------

def test_benchmark_passes_valid_package(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("coding", "builder", created_by="op",
                                bind=True)
    package = engine.get("builder")
    checks, summary = run_agent_benchmark(package)
    assert summary["total"] == len(checks)
    assert summary["passed"] == summary["total"]
    assert summary["pass_rate"] == 1.0
    assert summary["skipped"]  # live checks skipped, never assumed
    report = build_test_report(package)
    assert report["summary"]["meets_requirement"] is True


def test_benchmark_flags_dishonest_package(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("coding", "builder", created_by="op",
                                bind=False)
    package = engine.get("builder")
    package.real = True  # tamper: real without an executor
    package.executor = ""
    failed = [entry for entry in structural_checks(package)
              if not entry["passed"]]
    assert [entry["check"] for entry in failed] == ["binding-honest"]
    object.__setattr__(package.spec, "permissions",
                       ("read_file", "delete_repository"))
    failed = [entry["check"] for entry in structural_checks(package)
              if not entry["passed"]]
    assert "permissions-bounded" in failed


class MarkerProvider:
    """Deterministic provider that echoes the benchmark marker."""

    name = "marker"

    def generate(self, prompt, **kwargs):
        return ModelResult("FORGE-AGENT-OK", self.name)


def test_live_checks_are_opt_in(tmp_path):
    from forge.models.fabric import ModelFabric
    from forge.models.provider import ProviderRegistry
    from forge.models.registry import Model, ModelRegistry

    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(name="m/live", provider="p",
                  capabilities=("coding", "debugging", "testing"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": MarkerProvider()}),
    )
    engine = make_engine(tmp_path, fabric=fabric)
    engine.create_from_template("coding", "builder", created_by="op",
                                bind=True)
    package = engine.get("builder")
    _checks, summary = run_agent_benchmark(package, fabric)
    assert summary["skipped"]  # default: skipped
    live_checks, live_summary = run_agent_benchmark(
        package, fabric, live=True)
    assert live_summary["skipped"] == []
    assert live_summary["passed"] == live_summary["total"]
    assert any(entry["check"].startswith("live-")
               for entry in live_checks)


# -- no self-grant --------------------------------------------------------------------

def test_no_agent_may_self_grant(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("research", "scout", created_by="op",
                                bind=True)
    package = engine.get("scout")
    before = package.spec.permissions
    # Every power-changing operation refuses the agent-as-operator.
    with pytest.raises(SelfGrantDenied):
        engine.validate("scout", "scout")
    with pytest.raises(SelfGrantDenied):
        engine.test("scout", "scout")
    with pytest.raises(SelfGrantDenied):
        engine.enable("scout", "scout")
    with pytest.raises(SelfGrantDenied):
        engine.update_permissions("scout", ["read_file", "write_file"],
                                  "scout")
    with pytest.raises(SelfGrantDenied):
        engine.set_limits("scout", "scout", max_runs_per_hour=5)
    with pytest.raises(SelfGrantDenied):
        engine.update("scout", package.spec.to_dict(),
                      changed_by="scout")
    with pytest.raises(SelfGrantDenied):
        engine.pause("scout", "forge-agent:scout")
    with pytest.raises(SelfGrantDenied):
        engine.delete("scout", "scout")
    # Nothing changed.
    assert engine.get("scout").spec.permissions == before
    assert engine.get("scout").lifecycle == "created"
    # The operator path still works.
    engine.validate("scout", "op")
    assert engine.get("scout").lifecycle == "validated"


def test_self_grant_widens_nothing(tmp_path):
    engine = make_engine(tmp_path)
    enable_researcher(engine, "scout")
    with pytest.raises(SelfGrantDenied):
        engine.update_permissions(
            "scout", ["read_file", "write_file", "run_command"],
            "scout")
    assert "write_file" not in engine.get("scout").spec.permissions


# -- isolation --------------------------------------------------------------------------

def test_memory_namespaces_are_isolated(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("research", "agent-alpha",
                                created_by="op", bind=True)
    engine.create_from_template("research", "agent-beta",
                                created_by="op", bind=True)
    engine.memory_save("agent-alpha", "secret", "alpha-knows")
    assert engine.memory_load("agent-beta", "secret") is None
    assert engine.memory_list("agent-beta") == []
    assert engine.memory_list("agent-alpha") == ["secret"]
    assert engine.memory_load("agent-alpha", "secret") == "alpha-knows"


def test_memory_policy_enforced(tmp_path):
    spec = build_from_template("research", "forgetful")
    payload = spec.to_dict()
    payload["memory_policy"] = {
        "max_entries": 1, "max_value_bytes": 64,
        "allow_project_memory": False, "retention_days": 7}
    engine = make_engine(tmp_path)
    engine.create(payload, created_by="op", bind=True)
    engine.memory_save("forgetful", "one", "1")
    with pytest.raises(AgentEngineError, match="Memory limit"):
        engine.memory_save("forgetful", "two", "2")
    with pytest.raises(ValueError):
        engine.memory_save("forgetful", "one", "x" * 65)


def test_tool_and_permission_isolation(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("research", "scout", created_by="op",
                                bind=True)
    engine.create_from_template("coding", "builder",
                                created_by="op", bind=True)
    scout = engine.get("scout")
    manager = engine._scoped_manager(scout)
    from forge.security.permissions import PermissionLevel

    # Research allowlist: reads stay usable, writes are blocked.
    assert manager.check("read_file") == PermissionLevel.SAFE
    assert manager.check("write_file") == PermissionLevel.BLOCKED
    assert manager.check("run_command") == PermissionLevel.BLOCKED
    assert manager.check("delete_repository") == PermissionLevel.BLOCKED
    runtime = engine._scoped_runtime(scout, manager)
    names = {tool.name for tool in runtime.list_tools()}
    assert "write_file" not in names  # tool not even registered
    assert "terminal" not in names
    denied = runtime.execute("write_file", path="x.txt",
                             content="hi", approved=True,
                             actor="forge-agent:scout")
    assert denied.success is False
    unknown = runtime.execute("mind_control", approved=True)
    assert unknown.success is False
    assert "Unknown tool" in (unknown.error or "")
    # The coding agent is unaffected by the scout's boundaries.
    builder = engine.get("builder")
    bmanager = engine._scoped_manager(builder)
    assert bmanager.check("write_file") != PermissionLevel.BLOCKED
    bruntime = engine._scoped_runtime(builder, bmanager)
    assert "write_file" in {tool.name
                            for tool in bruntime.list_tools()}


def test_run_history_isolated_per_agent(tmp_path):
    engine = make_engine(tmp_path)
    enable_researcher(engine, "agent-alpha")
    enable_researcher(engine, "agent-beta")
    engine.execute("agent-alpha", "count files", actor="op")
    assert len(engine.history("agent-alpha")) == 1
    assert engine.history("agent-beta") == []


# -- permission boundaries -----------------------------------------------------------------

def test_write_without_approval_is_refused(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("coding", "builder", created_by="op",
                                bind=True)
    engine.validate("builder", "op")
    engine.test("builder", "op")
    engine.enable("builder", "op")
    result = engine.execute("builder", "add CSV export", actor="op")
    assert result["success"] is False
    assert result["error"]
    root = Path(engine.root)
    assert "export_csv" not in (root / "app.py").read_text()


def test_approved_write_applies_through_gates(tmp_path):
    engine = make_engine(tmp_path)
    engine.create_from_template("coding", "builder", created_by="op",
                                bind=True)
    engine.validate("builder", "op")
    engine.test("builder", "op")
    engine.enable("builder", "op")
    result = engine.execute("builder", "add CSV export", approved=True,
                            actor="op")
    assert result["success"] is True, result
    assert "app.py" in result["files"]
    root = Path(engine.root)
    assert "export_csv" in (root / "app.py").read_text()
    assert result["verification"]["security"]["passed"] is True


def test_governor_enforces_resource_limits(tmp_path):
    engine = make_engine(tmp_path)
    enable_researcher(engine, "scout")
    engine.set_limits("scout", "op", max_runs_per_hour=1)
    first = engine.execute("scout", "count files", actor="op")
    assert first["success"] is True
    with pytest.raises(AgentEngineError, match="hourly run limit"):
        engine.execute("scout", "count files again", actor="op")


# -- guarded execution through all six subsystems --------------------------------------------

def test_execute_wires_all_subsystems(tmp_path):
    from forge.models.fabric import ModelFabric
    from forge.models.provider import ProviderRegistry
    from forge.models.registry import Model, ModelRegistry

    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(name="m/research", provider="p",
                  capabilities=("research", "reasoning"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": ScriptedProvider()}),
    )
    engine = make_engine(tmp_path, fabric=fabric)
    enable_researcher(engine, "scout")
    result = engine.execute("scout", "count the python files",
                            actor="op")
    assert result["success"] is True
    assert '"python_files": 1' in result["output"]
    # Model Fabric: a real routing decision is recorded.
    assert result["fabric_route"]["routed"] is True
    assert result["model"] == "m/research"
    # Verification + Checkpoints: gates ran, checkpoint minted.
    assert set(result["verification"]) == {"security", "review"}
    assert result["checkpoint_id"]
    assert result["rolled_back"] is False
    assert result["elapsed_ms"] >= 0
    # Tool Runtime + PolicyGate + Memory: scoped and namespaced.
    package = engine.get("scout")
    manager = engine._scoped_manager(package)
    runtime = engine._scoped_runtime(package, manager)
    read = runtime.execute("read_file", path="app.py",
                           actor="forge-agent:scout")
    assert read.success is True
    assert "health" in read.output
    engine.memory_save("scout", "finding", "one python file")
    assert engine.memory_load("scout", "finding") == "one python file"


def test_failed_runs_roll_back(tmp_path):
    engine = make_engine(tmp_path)
    # Tamper the root with a secret after enabling, so the security
    # gate fails the run and the checkpoint path is exercised.
    enable_researcher(engine, "scout")
    root = Path(engine.root)
    (root / "leak.py").write_text('api_key = "supersecretvalue"\n')
    record = engine.execute("scout", "count files", actor="op")
    assert record["success"] is False
    assert record["verification"]["security"]["passed"] is False
    (root / "leak.py").unlink()


# -- persistent store --------------------------------------------------------------------------

def test_file_store_round_trip(tmp_path):
    store = AgentStore(tmp_path / "agents")
    engine = make_engine(tmp_path, store=store)
    engine.create_from_template("security", "guard", created_by="op",
                                bind=True)
    assert (tmp_path / "agents" / "guard.json").exists()
    fresh = AgentCreationEngine(root=str(tmp_path / "proj"),
                                store=AgentStore(tmp_path / "agents"))
    assert fresh.get("guard").spec.name == "guard"
    assert fresh.list()[0].lifecycle == "created"
    with pytest.raises(ValueError):
        store.load("../escape")


# -- CLI ------------------------------------------------------------------------------------------

def test_cli_agents_full_lifecycle(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    store = str(tmp_path / "agents")
    with pytest.raises(SystemExit) as created:
        run_cli(["forge", "agents", "create", "cli-coder",
                 "--template", "coding", "--bind",
                 "--store", store])
    assert created.value.code == 0
    assert "Created cli-coder" in capsys.readouterr().out

    with pytest.raises(SystemExit) as listed:
        run_cli(["forge", "agents", "--store", store])
    assert listed.value.code == 0
    assert "cli-coder" in capsys.readouterr().out

    for step in ("validate", "test", "enable"):
        with pytest.raises(SystemExit) as done:
            run_cli(["forge", "agents", step, "cli-coder",
                     "--store", store])
        assert done.value.code == 0, step
        capsys.readouterr()

    with pytest.raises(SystemExit) as disabled:
        run_cli(["forge", "agents", "disable", "cli-coder",
                 "--store", store, "--json"])
    assert disabled.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lifecycle"] == "disabled"

    with pytest.raises(SystemExit) as templates:
        run_cli(["forge", "agents", "templates", "--store", store])
    assert templates.value.code == 0
    assert "game-development" in capsys.readouterr().out


def test_cli_agents_rejects_bad_input(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    store = str(tmp_path / "agents")
    with pytest.raises(SystemExit) as bad:
        run_cli(["forge", "agents", "create", "bad!",
                 "--template", "coding", "--store", store])
    assert bad.value.code == 1
    with pytest.raises(SystemExit) as missing:
        run_cli(["forge", "agents", "enable", "ghost",
                 "--store", store])
    assert missing.value.code == 1


# -- control plane -------------------------------------------------------------------------------

def test_plane_engine_lifecycle_and_run(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=RUN_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.spec_agent_create(
            session, {"template": "research", "name": "scout"},
            bind=True)
        assert created["lifecycle"] == "created"
        assert created["real"] is True
        plane.spec_agent_validate(session, "scout")
        tested = plane.spec_agent_test(session, "scout")
        assert tested["report"]["summary"]["meets_requirement"] is True
        enabled = plane.spec_agent_enable(session, "scout")
        assert enabled["lifecycle"] == "enabled"
        started = plane.spec_agent_run(session, "scout",
                                       "count the python files")
        assert started["allowed"] is True
        deadline = time.time() + 30.0
        while True:
            state = plane.spec_agent_run_result(
                session, "scout", started["run_id"])
            if state["status"] != "pending":
                break
            assert time.time() < deadline, "engine run never finished"
            time.sleep(0.05)
        assert state["status"] == "finished", state
        assert '"python_files": 2' in state["run"]["output"]
        runs = plane.spec_agent_runs(session, "scout")["runs"]
        assert len(runs) == 1
        versions = plane.spec_agent_versions(session, "scout")
        assert versions["version"] == "1.0.0"
        plane.spec_agent_disable(session, "scout")
        plane.spec_agent_retire(session, "scout")
        assert plane.spec_agent_get(
            session, "scout")["lifecycle"] == "retired"


def test_plane_engine_sessions_are_isolated(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=RUN_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        first, _t1, _h1 = login(client, actor="ada")
        second, _t2, _h2 = login(client, actor="grace")
        session_a = plane.sessions.get(first["session_id"])
        session_b = plane.sessions.get(second["session_id"])
        plane.spec_agent_create(
            session_a, {"template": "research", "name": "scout"},
            bind=True)
        assert plane.spec_agent_list(session_a)["agents"]
        assert plane.spec_agent_list(session_b)["agents"] == []
        with pytest.raises(Exception):
            plane.spec_agent_get(session_b, "scout")


def test_plane_engine_run_denied_fails_closed(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=DENY_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.spec_agent_create(
            session, {"template": "research", "name": "scout"},
            bind=True)
        plane.spec_agent_validate(session, "scout")
        plane.spec_agent_test(session, "scout")
        plane.spec_agent_enable(session, "scout")
        refused = plane.spec_agent_run(session, "scout",
                                       "count files")
        assert refused["allowed"] is False
        assert refused["run_id"] == ""
        assert plane.spec_agent_runs(session, "scout")["runs"] == []


def test_plane_engine_name_collision_with_legacy(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=RUN_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.agent_create(session, "dupe", "coding", ["coding"])
        with pytest.raises(Exception, match="already exists"):
            plane.spec_agent_create(
                session, {"template": "coding", "name": "dupe"})
        plane.spec_agent_create(
            session, {"template": "coding", "name": "fresh"})
        with pytest.raises(Exception, match="already exists"):
            plane.agent_create(session, "fresh", "coding",
                               ["coding"])


def test_engine_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=RUN_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/agent-specs").status_code == 401
        _session, _token, headers = login(client)
        templates = client.get("/api/v1/agent-specs/templates",
                               headers=headers)
        assert templates.status_code == 200
        assert len(templates.json()["templates"]) == 6
        created = client.post(
            "/api/v1/agent-specs", headers=headers,
            json={"spec": {"template": "security",
                           "name": "guard"},
                  "bind": True})
        assert created.status_code == 200, created.text
        assert created.json()["lifecycle"] == "created"
        bad = client.post(
            "/api/v1/agent-specs", headers=headers,
            json={"spec": {"template": "nope", "name": "bad"}})
        assert bad.status_code == 400
        listed = client.get("/api/v1/agent-specs", headers=headers)
        assert [item["spec"]["name"]
                for item in listed.json()["agents"]] == ["guard"]
        assert client.post("/api/v1/agent-specs/guard/validate",
                           headers=headers).status_code == 200
        tested = client.post("/api/v1/agent-specs/guard/test",
                             headers=headers)
        assert tested.status_code == 200
        assert tested.json()["report"]["summary"][
            "meets_requirement"] is True
        enabled = client.post("/api/v1/agent-specs/guard/enable",
                              headers=headers)
        assert enabled.status_code == 200
        assert enabled.json()["lifecycle"] == "enabled"
        versions = client.get("/api/v1/agent-specs/guard/versions",
                              headers=headers)
        assert versions.status_code == 200
        assert versions.json()["version"] == "1.0.0"
        perms = client.put("/api/v1/agent-specs/guard/permissions",
                           headers=headers,
                           json={"permissions": ["read_file"]})
        assert perms.status_code == 200
        assert perms.json()["spec"]["permissions"] == ["read_file"]


# -- desktop backend (Agent Manager) ---------------------------------------------------------------

def test_desktop_agent_manager(tmp_path):
    from forge.desktop_app.backend import DesktopBackend

    root = tmp_path / "demo"
    root.mkdir()
    (root / "app.py").write_text("def health(): return True\n")
    fabric = make_fabric(ScriptedProvider())
    backend = DesktopBackend(actor="tester",
                             db_path=str(tmp_path / "desktop.db"),
                             fabric=fabric, approval_timeout=30.0)
    backend.start({"demo": str(root)})
    try:
        templates = backend.agent_templates("demo")
        assert {entry["template"] for entry in templates} == set(
            TEMPLATE_NAMES)
        created = backend.create_agent("demo", "documentation",
                                       "scribe", bind=True)
        assert created["lifecycle"] == "created"
        assert [item["spec"]["name"]
                for item in backend.list_agents("demo")] == ["scribe"]
        backend.validate_agent("demo", "scribe")
        report = backend.test_agent("demo", "scribe")
        assert report["report"]["summary"][
            "meets_requirement"] is True
        assert backend.enable_agent(
            "demo", "scribe")["lifecycle"] == "enabled"
        assert backend.disable_agent(
            "demo", "scribe")["lifecycle"] == "disabled"
        assert backend.agent_versions(
            "demo", "scribe")["version"] == "1.0.0"
        backend.delete_agent("demo", "scribe")
        assert backend.list_agents("demo") == []
    finally:
        backend.stop()
