"""First-party Forge Agent Creation Engine: specs, factory, lifecycle,
templates, benchmarks, versioning, CLI, desktop, isolation, and
permission boundaries."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (ScriptedProvider, login, make_client, make_fabric,  # noqa: E402
                         make_plane, make_repo)

from forge.agents import lifecycle  # noqa: E402
from forge.agents.agent_benchmark import run_agent_benchmark  # noqa: E402
from forge.agents.creation_engine import AgentCreationEngine  # noqa: E402
from forge.agents.managed_executor import (ManagedExecutionError,  # noqa: E402
                                           execute_managed_agent)
from forge.agents.package import (AgentPackage, bump_version,  # noqa: E402
                                  compare_versions, parse_version)
from forge.agents.spec import AgentSpec, validate_spec_dict  # noqa: E402
from forge.agents.templates import (build_spec, get_template,  # noqa: E402
                                    list_templates, template_names)
from forge.security.policy import (PermissionPolicy, PermissionRule,  # noqa: E402
                                   Resource)


def _coding_spec(name: str = "test-coder") -> dict:
    return build_spec("coding", name)


# -- 1. agent specification: nine validated dimensions ------------------------


def test_spec_requires_all_nine_dimensions():
    spec = validate_spec_dict(_coding_spec())
    assert spec.name == "test-coder"
    assert spec.purpose
    assert spec.capabilities
    assert spec.tools
    assert spec.permissions
    assert spec.model_requirements.capability == "coding"
    assert spec.memory_policy.isolated is True
    assert spec.verification_requirements
    assert spec.resource_limits.max_runs_per_hour >= 1


def test_spec_rejects_bad_identity_and_purpose():
    bad = _coding_spec("Bad Name!")
    with pytest.raises(ValueError):
        validate_spec_dict(bad)
    bad = _coding_spec()
    bad["purpose"] = ""
    with pytest.raises(ValueError):
        validate_spec_dict(bad)
    bad = _coding_spec()
    bad["capabilities"] = ["mind-reading"]
    with pytest.raises(ValueError):
        validate_spec_dict(bad)
    bad = _coding_spec()
    bad["capabilities"] = []
    with pytest.raises(ValueError):
        validate_spec_dict(bad)


def test_spec_rejects_unknown_tools_and_gates():
    bad = _coding_spec()
    bad["tools"] = ["read_file", "teleport"]
    with pytest.raises(ValueError):
        validate_spec_dict(bad)
    bad = _coding_spec()
    bad["verification_requirements"] = ["tests", "telepathy"]
    with pytest.raises(ValueError):
        validate_spec_dict(bad)
    bad = _coding_spec()
    bad["verification_requirements"] = []
    with pytest.raises(ValueError):
        validate_spec_dict(bad)


def test_spec_rejects_bad_permissions():
    bad = _coding_spec()
    bad["permissions"] = [{"resource": "starship", "operation": "fly"}]
    with pytest.raises(ValueError):
        validate_spec_dict(bad)
    bad = _coding_spec()
    bad["permissions"] = [{"resource": "filesystem", "operation": "fly"}]
    with pytest.raises(ValueError):
        validate_spec_dict(bad)
    # Blank terminal scopes are unbounded shell grants: rejected.
    bad = _coding_spec()
    bad["permissions"] = [{"resource": "terminal", "operation": "execute",
                           "scope": ""}]
    with pytest.raises(ValueError):
        validate_spec_dict(bad)


def test_spec_rejects_relaxed_memory_and_bad_limits():
    bad = _coding_spec()
    bad["memory_policy"] = {"max_entries": 200, "max_value_chars": 2000,
                            "isolated": False}
    with pytest.raises(ValueError):
        validate_spec_dict(bad)
    bad = _coding_spec()
    bad["resource_limits"] = {"max_runs_per_hour": 99999,
                              "max_concurrent": 2, "max_seconds": 300}
    with pytest.raises(ValueError):
        validate_spec_dict(bad)
    bad = _coding_spec()
    bad["model_requirements"] = {"capability": "telepathy"}
    with pytest.raises(ValueError):
        validate_spec_dict(bad)


def test_spec_dedupes_and_bounds():
    spec_dict = _coding_spec()
    spec_dict["capabilities"] = ["coding", "coding", "testing"]
    spec = validate_spec_dict(spec_dict)
    assert spec.capabilities == ("coding", "testing")
    spec_dict = _coding_spec()
    spec_dict["capabilities"] = ["coding"] * 20
    # dedup collapses to one, so this passes; oversized distinct lists fail
    assert validate_spec_dict(spec_dict).capabilities == ("coding",)
    assert len(AgentSpec.from_dict(spec_dict).capabilities) == 1


# -- 2. factory generates structured packages ---------------------------------


def test_factory_creates_versioned_package():
    engine = AgentCreationEngine("s1")
    package = engine.create(_coding_spec(), "alice")
    assert isinstance(package, AgentPackage)
    assert package.name == "test-coder"
    assert package.version == "1.0.0"
    assert package.state == lifecycle.CREATED
    assert package.created_by == "alice"
    assert package.history  # creation recorded
    payload = package.to_dict()
    assert payload["format"] == "forge-agent-package"
    assert payload["spec"]["name"] == "test-coder"
    # Roundtrip through plain data.
    assert AgentPackage.from_dict(json.loads(json.dumps(payload))).name == \
        "test-coder"


def test_factory_rejects_duplicates_and_bad_actors():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    with pytest.raises(ValueError):
        engine.create(_coding_spec(), "alice")
    with pytest.raises(ValueError):
        engine.create(_coding_spec("other-agent"), "")
    with pytest.raises(ValueError):
        engine.create(_coding_spec("selfish"), "selfish")


def test_factory_persists_packages(tmp_path):
    store = tmp_path / "agents"
    engine = AgentCreationEngine("cli", store_dir=store)
    engine.create(_coding_spec(), "alice")
    assert (store / "test-coder.json").exists()
    reopened = AgentCreationEngine("cli", store_dir=store)
    assert reopened.get("test-coder") is not None
    assert reopened.get("test-coder").version == "1.0.0"


# -- 3. lifecycle: created/validated/tested/enabled/paused/disabled/retired ---


def test_lifecycle_all_seven_states():
    assert set(lifecycle.ALL_STATES) == {
        "created", "validated", "tested", "enabled",
        "paused", "disabled", "retired"}


def test_lifecycle_full_walk():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    assert engine.validate("test-coder", "alice")["state"] == "validated"
    report = engine.test("test-coder", "alice")
    assert report["success"] is True
    assert engine.get("test-coder").state == "tested"
    assert engine.enable("test-coder", "alice").state == "enabled"
    assert engine.pause("test-coder", "alice").state == "paused"
    assert engine.enable("test-coder", "alice").state == "enabled"
    assert engine.disable("test-coder", "alice").state == "disabled"
    assert engine.enable("test-coder", "alice").state == "enabled"
    assert engine.retire("test-coder", "alice").state == "retired"


def test_lifecycle_refuses_skips_and_resurrection():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    with pytest.raises(ValueError):
        engine.enable("test-coder", "alice")  # created -> enabled skips steps
    with pytest.raises(ValueError):
        engine.test("test-coder", "alice")  # created -> tested skips validate
    engine.validate("test-coder", "alice")
    with pytest.raises(ValueError):
        engine.enable("test-coder", "alice")  # validated -> enabled skips test
    engine.test("test-coder", "alice")
    engine.enable("test-coder", "alice")
    engine.retire("test-coder", "alice")
    with pytest.raises(ValueError):
        engine.enable("test-coder", "alice")  # retired is terminal
    with pytest.raises(ValueError):
        engine.transition("test-coder", "created", "alice")


def test_lifecycle_pause_disable_rules():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    engine.validate("test-coder", "alice")
    engine.test("test-coder", "alice")
    with pytest.raises(ValueError):
        engine.pause("test-coder", "alice")  # tested cannot pause
    with pytest.raises(ValueError):
        engine.disable("test-coder", "alice")  # tested cannot disable
    engine.enable("test-coder", "alice")
    engine.pause("test-coder", "alice")
    engine.disable("test-coder", "alice")  # paused -> disabled is legal
    assert engine.get("test-coder").state == "disabled"


# -- 4. operation through the six subsystems ----------------------------------


class _StubGate:
    def __init__(self, passed=True):
        self._passed = passed

    def __call__(self):
        from forge.security.verification import GateResult

        return GateResult("stub", self._passed, "stub evidence")


class _StubVerifier:
    def __init__(self, passed=True):
        self._passed = passed
        self.calls: list[str] = []

    def __getattr__(self, name):
        def _gate(*args, **kwargs):
            from forge.security.verification import GateResult

            self.calls.append(name)
            return GateResult(name, self._passed, "stub")
        return _gate


class _StubCheckpointManager:
    def __init__(self):
        self.created: list[str] = []

    def create(self, label="change", declared=None):
        from forge.tools.checkpoint import Checkpoint
        from pathlib import Path as _Path
        import tempfile as _tempfile

        snapshot = _Path(_tempfile.mkdtemp(prefix="forge-stub-"))
        checkpoint = Checkpoint(f"ckpt-{len(self.created)}", _Path("."),
                                snapshot, {})
        self.created.append(label)
        return checkpoint


def _enabled_package(engine=None):
    engine = engine or AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    engine.validate("test-coder", "alice")
    engine.test("test-coder", "alice")
    engine.enable("test-coder", "alice")
    return engine.require("test-coder")


def test_executor_requires_enabled_state():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    package = engine.require("test-coder")
    with pytest.raises(ManagedExecutionError):
        execute_managed_agent(package, "do work")


def test_executor_uses_fabric_memory_and_verification(tmp_path):
    from forge.agents.memory import AgentMemoryStore
    from forge.control.db import Database

    package = _enabled_package()
    fabric = make_fabric(ScriptedProvider())
    db = Database(str(tmp_path / "mem.db"))
    memory = AgentMemoryStore(db)
    verifier = _StubVerifier(passed=True)
    result = execute_managed_agent(
        package, "summarize the repo", fabric=fabric,
        memory_store=memory, verifier=verifier)
    assert result["success"] is True
    evidence = result["evidence"]
    assert evidence["model"]["success"] is True
    assert evidence["model"]["model"] == "m/a34"
    assert evidence["memory"]["namespace"] == "test-coder"
    assert evidence["memory"]["isolated"] is True
    assert evidence["verification"]["passed"] is True
    assert set(verifier.calls) == set(package.spec.verification_requirements)
    # Run record landed in this agent's namespace only.
    assert len(memory.list("test-coder")) == 1
    assert memory.list("other-agent") == []


def test_executor_verification_failure_blocks_run(tmp_path):
    from forge.agents.memory import AgentMemoryStore
    from forge.control.db import Database

    package = _enabled_package()
    db = Database(str(tmp_path / "mem.db"))
    with pytest.raises(ManagedExecutionError) as exc:
        execute_managed_agent(
            package, "do work", fabric=make_fabric(ScriptedProvider()),
            memory_store=AgentMemoryStore(db),
            verifier=_StubVerifier(passed=False))
    assert "Verification failed" in str(exc.value)


def test_executor_enforces_tool_allowlist(tmp_path):
    from forge.agents.memory import AgentMemoryStore
    from forge.control.db import Database
    from forge.runtime.defaults import create_default_runtime
    from forge.security.permissions import PermissionManager

    package = _enabled_package()
    db = Database(str(tmp_path / "mem.db"))
    runtime = create_default_runtime(PermissionManager(), str(tmp_path))
    with pytest.raises(ManagedExecutionError) as exc:
        execute_managed_agent(
            package, "do work", fabric=make_fabric(ScriptedProvider()),
            runtime=runtime,
            tool_calls=[{"tool": "desktop", "args": {}}],
            memory_store=AgentMemoryStore(db))
    assert "allowlist" in str(exc.value)


def test_executor_runs_allowlisted_read_tool(tmp_path):
    from forge.agents.memory import AgentMemoryStore
    from forge.control.db import Database
    from forge.runtime.defaults import create_default_runtime
    from forge.security.permissions import PermissionManager

    (tmp_path / "app.py").write_text("def health(): return True\n")
    package = _enabled_package()
    db = Database(str(tmp_path / "mem.db"))
    runtime = create_default_runtime(PermissionManager(), str(tmp_path))
    result = execute_managed_agent(
        package, "read the app", fabric=make_fabric(ScriptedProvider()),
        runtime=runtime,
        tool_calls=[{"tool": "read_file", "args": {"path": "app.py"}}],
        memory_store=AgentMemoryStore(db))
    assert result["success"] is True
    calls = result["evidence"]["tools"]["calls"]
    assert calls[0]["tool"] == "read_file"
    assert "health" in calls[0]["output"]


def test_executor_checkpoints_before_writes(tmp_path):
    from forge.agents.memory import AgentMemoryStore
    from forge.control.db import Database
    from forge.runtime.defaults import create_default_runtime
    from forge.security.permissions import OperationMode, PermissionManager
    from forge.security.policy_gate import PolicyGate

    package = _enabled_package()
    db = Database(str(tmp_path / "mem.db"))
    permissions = PermissionManager(mode=OperationMode.AUTONOMOUS)
    runtime = create_default_runtime(permissions, str(tmp_path))
    checkpoints = _StubCheckpointManager()
    result = execute_managed_agent(
        package, "write a note", fabric=make_fabric(ScriptedProvider()),
        policy_gate=PolicyGate(permissions), runtime=runtime,
        tool_calls=[{"tool": "write_file",
                     "args": {"path": "note.md", "content": "hi"}}],
        memory_store=AgentMemoryStore(db),
        checkpoint_manager=checkpoints)
    assert result["success"] is True
    assert result["evidence"]["checkpoint"]["id"].startswith("ckpt-")
    assert checkpoints.created  # snapshot taken before the write
    assert (tmp_path / "note.md").read_text() == "hi"


def test_executor_policy_gate_denies_unapproved_writes(tmp_path):
    from forge.agents.memory import AgentMemoryStore
    from forge.control.db import Database
    from forge.runtime.defaults import create_default_runtime
    from forge.security.permissions import PermissionManager
    from forge.security.policy_gate import PolicyGate

    package = _enabled_package()
    db = Database(str(tmp_path / "mem.db"))
    permissions = PermissionManager()  # ASSISTED: writes need approval
    runtime = create_default_runtime(permissions, str(tmp_path))
    with pytest.raises(ManagedExecutionError) as exc:
        execute_managed_agent(
            package, "write a note",
            fabric=make_fabric(ScriptedProvider()),
            policy_gate=PolicyGate(permissions), runtime=runtime,
            tool_calls=[{"tool": "write_file",
                         "args": {"path": "note.md", "content": "hi"}}],
            memory_store=AgentMemoryStore(db))
    assert "PolicyGate refused" in str(exc.value)
    assert not (tmp_path / "note.md").exists()


def test_executor_enforces_quotas():
    from forge.agents.governance import AgentGovernor

    package = _enabled_package()
    governor = AgentGovernor()
    governor.set_limits("test-coder", max_runs_per_hour=100,
                        max_concurrent=1)
    governor.begin("test-coder")  # occupy the single slot
    try:
        with pytest.raises(ManagedExecutionError) as exc:
            execute_managed_agent(
                package, "do work",
                fabric=make_fabric(ScriptedProvider()), governor=governor)
        assert "concurrency limit" in str(exc.value)
    finally:
        governor.end("test-coder")


# -- 5. no agent may self-grant permissions ------------------------------------


def test_agent_cannot_self_grant():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    grant = {"resource": "filesystem", "operation": "write",
             "scope": "secrets/**"}
    for actor in ("test-coder", "forge-managed:test-coder",
                  "agent:test-coder"):
        with pytest.raises(ValueError) as exc:
            engine.grant_permission("test-coder", grant, actor)
        assert "self" in str(exc.value).lower()
    # The spec is unchanged after refused self-grants.
    assert len(engine.require("test-coder").spec.permissions) == 5


def test_agent_cannot_self_transition_or_update():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    with pytest.raises(ValueError):
        engine.transition("test-coder", "validated", "test-coder")
    with pytest.raises(ValueError):
        engine.validate("test-coder", "test-coder")
    with pytest.raises(ValueError):
        engine.update("test-coder", _coding_spec(), "test-coder")
    with pytest.raises(ValueError):
        engine.bump("test-coder", "test-coder")
    assert engine.require("test-coder").state == "created"


def test_operator_grant_bumps_version_and_resets_lifecycle():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    engine.validate("test-coder", "alice")
    engine.test("test-coder", "alice")
    engine.enable("test-coder", "alice")
    package = engine.grant_permission(
        "test-coder",
        {"resource": "filesystem", "operation": "read", "scope": "docs/**"},
        "alice")
    assert package.version == "1.0.1"
    assert package.state == "created"  # must re-earn trust
    assert len(package.spec.permissions) == 6


def test_request_permission_never_grants():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    receipt = engine.request_permission(
        "test-coder",
        {"resource": "filesystem", "operation": "write",
         "scope": "extra/**"},
        requested_by="test-coder")
    assert receipt["granted"] is False
    assert len(engine.require("test-coder").spec.permissions) == 5


def test_grant_shaped_tools_are_refused():
    package = _enabled_package()
    for tool in ("grant_admin", "approve_request", "escalate_privs"):
        with pytest.raises(ManagedExecutionError) as exc:
            execute_managed_agent(
                package, "become admin",
                fabric=make_fabric(ScriptedProvider()),
                tool_calls=[{"tool": tool, "args": {}}])
        assert "never grant permissions" in str(exc.value)


# -- 6. templates ---------------------------------------------------------------


def test_all_six_templates_validate():
    assert template_names() == ["coding", "research", "security", "game-dev",
                                "os-dev", "documentation"]
    assert len(list_templates()) == 6
    for name in template_names():
        spec = validate_spec_dict(build_spec(name, f"{name}-agent"))
        assert spec.name == f"{name}-agent"
        assert spec.purpose


def test_templates_differ_sensibly():
    coding = get_template("coding")
    security = get_template("security")
    assert "write_file" in coding["tools"]
    assert "write_file" not in security["tools"]
    docs = get_template("documentation")
    assert docs["permissions"][1]["scope"] == "docs/**"
    with pytest.raises(ValueError):
        get_template("teleportation")


def test_engine_creates_from_every_template():
    engine = AgentCreationEngine("s1")
    for name in template_names():
        agent = f"{name}-agent".replace("-", "_").replace("_", "-")
        package = engine.create_from_template(name, agent, "alice")
        assert package.provenance["template"] == name
        assert package.state == "created"


# -- 7. benchmark testing --------------------------------------------------------


def test_benchmark_reports_all_checks():
    package = _enabled_package()
    report = run_agent_benchmark(package, make_fabric(ScriptedProvider()))
    assert report["total"] == 8
    assert report["passed"] == 8
    assert report["success"] is True
    assert report["honest"] is True
    names = {entry["check"] for entry in report["checks"]}
    assert names == {"spec_valid", "lifecycle_valid", "tools_allowlisted",
                     "permissions_bounded", "memory_isolated",
                     "model_routable", "verification_declared",
                     "resource_limits_bounded"}


def test_benchmark_detects_unroutable_capability():
    engine = AgentCreationEngine("s1")
    engine.create_from_template("security", "sec-agent", "alice")
    package = engine.require("sec-agent")
    # The test fabric serves coding/debugging/review only: security is
    # honestly reported as unroutable.
    report = run_agent_benchmark(package, make_fabric(ScriptedProvider()))
    routed = next(entry for entry in report["checks"]
                  if entry["check"] == "model_routable")
    assert routed["passed"] is False
    assert report["success"] is False


def test_benchmark_without_fabric_skips_routing_honestly():
    package = _enabled_package()
    report = run_agent_benchmark(package, None)
    routed = next(entry for entry in report["checks"]
                  if entry["check"] == "model_routable")
    assert routed["passed"] is True
    assert "skipped" in routed["detail"]


def test_engine_test_records_evidence_and_gates_enablement():
    engine = AgentCreationEngine("s1")
    engine.create_from_template("security", "sec-agent", "alice")
    engine.validate("sec-agent", "alice")
    report = engine.test("sec-agent", "alice",
                         make_fabric(ScriptedProvider()))
    assert report["success"] is False
    assert engine.get("sec-agent").state == "validated"  # not tested
    assert engine.get("sec-agent").benchmarks  # failure recorded
    with pytest.raises(ValueError):
        engine.enable("sec-agent", "alice")


# -- 8. versioning -----------------------------------------------------------------


def test_semver_helpers():
    assert parse_version("1.2.3") == (1, 2, 3)
    with pytest.raises(ValueError):
        parse_version("1.2")
    with pytest.raises(ValueError):
        parse_version("v1.2.3")
    assert bump_version("1.2.3", "patch") == "1.2.4"
    assert bump_version("1.2.3", "minor") == "1.3.0"
    assert bump_version("1.2.3", "major") == "2.0.0"
    assert compare_versions("1.0.0", "1.0.1") == -1
    assert compare_versions("2.0.0", "1.9.9") == 1
    assert compare_versions("1.0.0", "1.0.0") == 0


def test_version_history_is_preserved():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    engine.validate("test-coder", "alice")
    engine.test("test-coder", "alice")
    engine.enable("test-coder", "alice")
    versions = [entry["version"]
                for entry in engine.require("test-coder").history]
    assert versions[0] == "1.0.0"
    engine.bump("test-coder", "alice", "minor")
    package = engine.require("test-coder")
    assert package.version == "1.1.0"
    assert package.state == "created"
    assert [entry["version"] for entry in package.history][0] == "1.0.0"
    with pytest.raises(ValueError):
        engine.set_version("test-coder", "1.0.0", "alice")  # no downgrades
    with pytest.raises(ValueError):
        engine.set_version("test-coder", "not-a-version", "alice")


def test_spec_update_requires_revalidation():
    engine = AgentCreationEngine("s1")
    engine.create(_coding_spec(), "alice")
    engine.validate("test-coder", "alice")
    engine.test("test-coder", "alice")
    updated = dict(_coding_spec())
    updated["purpose"] = "A refined purpose for the same coder."
    package = engine.update("test-coder", updated, "alice")
    assert package.version == "1.0.1"
    assert package.state == "created"
    assert package.spec.purpose.startswith("A refined purpose")


# -- control plane + API -----------------------------------------------------------


def _managed_policy(*agents: str) -> PermissionPolicy:
    rules = [PermissionRule(id=f"a{i}", resource=Resource.AGENT,
                            operation="execute", scope=name, effect="ALLOW")
             for i, name in enumerate(agents)]
    return PermissionPolicy(rules=rules)


def test_plane_managed_lifecycle_and_audit(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=_managed_policy("plane-coder"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.managed_create_from_template(
            session, "coding", "plane-coder", "")
        assert created["state"] == "created"
        plane.managed_validate(session, "plane-coder")
        report = plane.managed_test(session, "plane-coder")
        assert report["success"] is True
        assert plane.managed_enable(session, "plane-coder")["state"] == \
            "enabled"
        assert plane.managed_disable(session, "plane-coder")["state"] == \
            "disabled"
        assert plane.managed_enable(session, "plane-coder")["state"] == \
            "enabled"
        listed = plane.managed_list(session)
        assert [item["name"] for item in listed["agents"]] == ["plane-coder"]
        audited = plane.audit.query(resource="managed-agents")
        assert {event.operation for event in audited} >= {
            "create", "validate", "test", "enable", "disable"}


def test_plane_managed_run_uses_all_subsystems(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=_managed_policy("runner"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.managed_create_from_template(session, "coding", "runner", "")
        plane.managed_validate(session, "runner")
        assert plane.managed_test(session, "runner")["success"] is True
        plane.managed_enable(session, "runner")
        result = plane.managed_run(session, "runner", "summarize the repo")
        assert result["allowed"] is True
        run = result["run"]
        assert run["success"] is True
        evidence = run["evidence"]
        assert evidence["model"]["model"] == "m/a34"
        assert evidence["policy"]["decision"] == "ALLOW"
        assert evidence["tools"]["allowlist"]
        assert evidence["memory"]["namespace"] == "runner"
        assert evidence["verification"]["passed"] is True
        assert "checkpoint" in evidence
        assert "governor" in evidence


def test_plane_managed_run_files_approval_when_gated(tmp_path):
    plane = make_plane(tmp_path, start=True)  # default: fail-closed policy
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        plane.managed_create_from_template(session, "coding", "gated", "")
        plane.managed_validate(session, "gated")
        plane.managed_test(session, "gated")
        plane.managed_enable(session, "gated")
        result = plane.managed_run(session, "gated", "do work")
        # Default policy denies AGENT/execute: no run, honest reason.
        assert result["allowed"] is False
        assert result["run"] is None


def test_managed_api_flow(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=_managed_policy("api-coder"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/agents/managed").status_code == 401
        _session, _token, headers = login(client)
        templates = client.get("/api/v1/agents/managed/templates",
                               headers=headers)
        assert templates.status_code == 200
        assert len(templates.json()["templates"]) == 6
        created = client.post(
            "/api/v1/agents/managed/from-template", headers=headers,
            json={"template": "coding", "name": "api-coder"})
        assert created.status_code == 200, created.text
        assert created.json()["state"] == "created"
        bad = client.post(
            "/api/v1/agents/managed/from-template", headers=headers,
            json={"template": "nope", "name": "bad-agent"})
        assert bad.status_code == 400
        validated = client.post(
            "/api/v1/agents/managed/api-coder/validate", headers=headers)
        assert validated.status_code == 200
        tested = client.post("/api/v1/agents/managed/api-coder/test",
                             headers=headers)
        assert tested.status_code == 200
        assert tested.json()["success"] is True
        enabled = client.post("/api/v1/agents/managed/api-coder/enable",
                              headers=headers)
        assert enabled.status_code == 200
        assert enabled.json()["state"] == "enabled"
        disabled = client.post("/api/v1/agents/managed/api-coder/disable",
                               headers=headers)
        assert disabled.status_code == 200
        assert disabled.json()["state"] == "disabled"
        shown = client.get("/api/v1/agents/managed/api-coder",
                           headers=headers)
        assert shown.status_code == 200
        assert shown.json()["version"] == "1.0.0"
        versioned = client.post(
            "/api/v1/agents/managed/api-coder/version", headers=headers,
            json={"bump": "minor"})
        assert versioned.status_code == 200
        assert versioned.json()["version"] == "1.1.0"


# -- 11. isolation and permission boundaries ---------------------------------------


def test_memory_isolation_between_agents(tmp_path):
    from forge.agents.memory import AgentMemoryStore
    from forge.control.db import Database

    db = Database(str(tmp_path / "mem.db"))
    memory = AgentMemoryStore(db)
    fabric = make_fabric(ScriptedProvider())
    engine = AgentCreationEngine("s1")
    for name in ("agent-one", "agent-two"):
        engine.create(_coding_spec(name), "alice")
        engine.validate(name, "alice")
        engine.test(name, "alice")
        engine.enable(name, "alice")
    execute_managed_agent(engine.require("agent-one"), "private thought",
                          fabric=fabric, memory_store=memory)
    assert len(memory.list("agent-one")) == 1
    assert memory.list("agent-two") == []
    assert memory.get("agent-two", "runs") is None
    # Even with the key in hand, namespaces never overlap: agent-two's
    # read of agent-one's key path returns nothing.
    one_key = memory.list("agent-one")[0]["key"]
    assert memory.get("agent-two", one_key) is None


def test_session_isolation_between_engines(tmp_path):
    plane = make_plane(tmp_path, start=True,
                       policy=_managed_policy("sess-agent"))
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload_a, _t1, _h1 = login(client, actor="alice")
        payload_b, _t2, _h2 = login(client, actor="bob")
        session_a = plane.sessions.get(payload_a["session_id"])
        session_b = plane.sessions.get(payload_b["session_id"])
        plane.managed_create_from_template(session_a, "coding",
                                           "sess-agent", "")
        assert plane.managed_list(session_a)["agents"]
        assert plane.managed_list(session_b)["agents"] == []
        with pytest.raises(Exception):
            plane.managed_get(session_b, "sess-agent")


def test_permission_boundaries_deny_cross_agent_tools(tmp_path):
    from forge.agents.memory import AgentMemoryStore
    from forge.control.db import Database
    from forge.runtime.defaults import create_default_runtime
    from forge.security.permissions import PermissionManager

    db = Database(str(tmp_path / "mem.db"))
    engine = AgentCreationEngine("s1")
    # Research agents are read-only: no write_file in their allowlist.
    engine.create_from_template("research", "reader", "alice")
    engine.validate("reader", "alice")
    engine.test("reader", "alice")
    engine.enable("reader", "alice")
    package = engine.require("reader")
    assert "write_file" not in package.spec.tools
    runtime = create_default_runtime(PermissionManager(), str(tmp_path))
    with pytest.raises(ManagedExecutionError):
        execute_managed_agent(
            package, "overwrite app", fabric=make_fabric(ScriptedProvider()),
            runtime=runtime,
            tool_calls=[{"tool": "write_file",
                         "args": {"path": "app.py", "content": "pwned"}}],
            memory_store=AgentMemoryStore(db))
    assert not (tmp_path / "app.py").exists()


def test_approval_store_bars_agent_self_approval():
    from forge.security.approvals import ApprovalRequest, ApprovalStore

    store = ApprovalStore()
    request = store.submit(ApprovalRequest(
        agent="test-coder", resource=Resource.AGENT, operation="execute",
        scopes=("test-coder",), task_id="t1"))
    with pytest.raises(ValueError) as exc:
        store.decide(request.id, True, "test-coder")
    assert "cannot approve its own request" in str(exc.value)


# -- 9. CLI ------------------------------------------------------------------------


def _cli_main(argv, monkeypatch, capsys):
    from forge.cli import main

    monkeypatch.setattr(sys, "argv", ["forge"] + argv)
    with pytest.raises(SystemExit) as exc:
        main()
    return exc.value.code, capsys.readouterr()


def test_cli_agents_create_validate_test_enable_disable(tmp_path, monkeypatch,
                                                        capsys):
    store = str(tmp_path / "agents")
    code, _out = _cli_main(
        ["agents", "create", "--template", "coding", "--name", "cli-coder",
         "--store", store, "--actor", "alice"], monkeypatch, capsys)
    assert code == 0
    assert (tmp_path / "agents" / "cli-coder.json").exists()
    code, _out = _cli_main(
        ["agents", "validate", "cli-coder", "--store", store,
         "--actor", "alice"], monkeypatch, capsys)
    assert code == 0
    code, out = _cli_main(
        ["agents", "test", "cli-coder", "--store", store, "--actor", "alice"],
        monkeypatch, capsys)
    assert code == 0
    assert "8/8 passed" in out.out
    code, _out = _cli_main(
        ["agents", "enable", "cli-coder", "--store", store, "--actor", "alice"],
        monkeypatch, capsys)
    assert code == 0
    code, out = _cli_main(["agents", "--store", store], monkeypatch, capsys)
    assert code == 0
    assert "cli-coder" in out.out
    assert "enabled" in out.out
    code, _out = _cli_main(
        ["agents", "disable", "cli-coder", "--store", store, "--actor", "alice"],
        monkeypatch, capsys)
    assert code == 0
    code, out = _cli_main(
        ["agents", "show", "cli-coder", "--store", store], monkeypatch, capsys)
    assert code == 0
    assert "disabled" in out.out


def test_cli_agents_rejects_unknown_template(tmp_path, monkeypatch, capsys):
    code, _out = _cli_main(
        ["agents", "create", "--template", "nope", "--name", "bad",
         "--store", str(tmp_path / "agents")], monkeypatch, capsys)
    assert code == 1


def test_cli_agents_templates_and_json(tmp_path, monkeypatch, capsys):
    code, out = _cli_main(["agents", "templates"], monkeypatch, capsys)
    assert code == 0
    for name in ("coding", "research", "security", "game-dev", "os-dev",
                 "documentation"):
        assert name in out.out
    code, out = _cli_main(["agents", "templates", "--json"], monkeypatch,
                          capsys)
    assert code == 0
    assert len(json.loads(out.out)["templates"]) == 6


# -- 10. desktop Agent Manager -------------------------------------------------------


def test_desktop_backend_agent_manager(tmp_path):
    from forge.desktop_app.backend import BackendError, DesktopBackend

    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "app.py").write_text("def health(): return True\n")
    backend = DesktopBackend(
        actor="tester", db_path=str(tmp_path / "desktop.db"),
        fabric=make_fabric(ScriptedProvider()),
        policy=_managed_policy("desk-coder"))
    backend.start({"demo": str(repo)})
    try:
        templates = backend.managed_templates("demo")
        assert len(templates) == 6
        created = backend.create_managed_agent(
            "demo", "coding", "desk-coder", "")
        assert created["state"] == "created"
        assert backend.validate_managed_agent(
            "demo", "desk-coder")["state"] == "validated"
        report = backend.test_managed_agent("demo", "desk-coder")
        assert report["success"] is True
        assert backend.enable_managed_agent(
            "demo", "desk-coder")["state"] == "enabled"
        assert backend.disable_managed_agent(
            "demo", "desk-coder")["state"] == "disabled"
        agents = backend.list_managed_agents("demo")
        assert [item["name"] for item in agents] == ["desk-coder"]
        with pytest.raises(BackendError):
            backend.get_managed_agent("demo", "ghost")
    finally:
        backend.stop()


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
    for widget in ("Frame", "Label", "Button", "Combobox", "PanedWindow",
                   "Notebook", "Scrollbar", "LabelFrame", "Entry"):
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

    app = app_module.ForgeDesktopApp(DesktopBackend())
    # Manager refuses without a backend, asks for a project, then renders.
    app._show_agent_manager()
    app.backend._started = True
    app.backend._plane = object()
    app._current_project.set("")
    app._show_agent_manager()
    app._current_project.set("demo")
    app._agent_project = "demo"
    app._agent_names = []
    app._apply_agent_result({"action": "test", "name": "desk-coder",
                             "result": {"passed": 8, "total": 8,
                                       "checks": []}})
    app._on_close()
