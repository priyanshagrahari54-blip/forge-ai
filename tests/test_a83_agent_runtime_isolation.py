"""A83 — runtime isolation and permission boundaries.

Every test here drives the real :class:`AgentRuntime`: a scripted model
behind the real Model Fabric, the real Tool Runtime over a real temporary
project, the real PolicyGate, the real MemoryStore, and the real
CheckpointManager. Nothing is stubbed out at the boundary being tested.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a83 import (  # noqa: E402
    AgentProvider,
    action_payload,
    make_agent_fabric,
    make_engine,
    make_project,
    ready_agent,
    write_action,
)

from forge.agents.engine import (  # noqa: E402
    AgentIsolationError,
    AgentLimitError,
    AgentPermissionError,
)


@pytest.fixture()
def project(tmp_path):
    return make_project(tmp_path / "demo")


@pytest.fixture()
def engine(project):
    return make_engine(project)


def _run(engine, name, task="add CSV export", **kwargs):
    return engine.run(name, task, actor="alice", **kwargs)


# -- lifecycle gating ----------------------------------------------------


def test_only_an_enabled_agent_can_run(engine):
    engine.create_from_template("coding", "exporter", actor="alice")
    engine.grant_spec("exporter", actor="alice")
    for state in ("created", "validated", "tested", "disabled", "paused",
                  "retired"):
        package = engine.get("exporter")
        package.lifecycle.state = state
        engine.store.write_manifest(package)
        result = _run(engine, "exporter")
        assert result["success"] is False
        assert result["stage"] == "lifecycle"
        assert state in result["error"]


def test_an_empty_task_is_refused_before_any_work(engine):
    ready_agent(engine)
    result = _run(engine, "worker", "   ")
    assert result["success"] is False
    assert result["stage"] == "input"
    assert result["actions"] == []


# -- the sandbox surface -------------------------------------------------


def test_the_sandbox_exposes_no_permission_api(engine):
    ready_agent(engine)
    package = engine.get("worker")
    sandbox = engine.runtime.sandbox(package, actor="alice")
    for forbidden in ("grant", "revoke", "grant_spec", "set_mode", "policy",
                      "manager", "store", "permission_manager",
                      "add_permission", "escalate"):
        assert not hasattr(sandbox, forbidden), forbidden
    assert set(sandbox.allowed_tools()) == set(package.spec.tool_names())


def test_undeclared_tools_are_refused(engine, project):
    ready_agent(engine)
    package = engine.get("worker")
    sandbox = engine.runtime.sandbox(package, actor="alice", approved=True)
    assert "terminal" not in package.spec.tool_names()
    outcome = sandbox.use_tool("terminal", command=["rm", "-rf", "/"])
    assert outcome["allowed"] is False
    assert "not in the specification" in outcome["reason"]
    assert sandbox.refusals and sandbox.refusals[-1]["tool"] == "terminal"


def test_tools_are_refused_when_ungranted(engine, project):
    """Declaring a tool in the spec is not the same as being granted it."""
    engine.create_from_template("coding", "worker", actor="alice")
    engine.validate("worker", actor="alice")
    engine.test("worker", actor="alice")
    engine.enable("worker", actor="alice")
    package = engine.get("worker")
    sandbox = engine.runtime.sandbox(package, actor="alice", approved=True)
    outcome = sandbox.use_tool("write_file", path="out.py", content="x = 1\n")
    assert outcome["allowed"] is False
    assert "not granted" in outcome["reason"]
    assert not (Path(project) / "out.py").exists()


def test_per_run_tool_call_limits_are_enforced(engine, project):
    engine.create_from_template(
        "coding", "worker", actor="alice",
        overrides={"tools": [{"name": "read_file", "max_calls": 1},
                             {"name": "write_file", "max_calls": 8}],
                   "permissions": {
                       "operations": ["read_file", "write_file"],
                       "mode_ceiling": "assisted"},
                   "verification": {"require_tests": False,
                                    "require_review": False}})
    engine.validate("worker", actor="alice")
    engine.grant_spec("worker", actor="alice")
    engine.test("worker", actor="alice")
    engine.enable("worker", actor="alice")
    sandbox = engine.runtime.sandbox(engine.get("worker"), actor="alice",
                                     approved=True)
    first = sandbox.use_tool("read_file", path="app.py")
    assert first["allowed"] is True
    second = sandbox.use_tool("read_file", path="app.py")
    assert second["allowed"] is False
    assert "call limit" in second["reason"]


# -- path and PolicyGate boundaries --------------------------------------


@pytest.mark.parametrize("path", [
    ".forge/agents/worker/agent.json",
    ".forge/evil.py",
    ".git/config",
    "../escape.py",
    "/etc/passwd",
    "config/credentials.json",
    "secrets/notes.py",
    "deploy/.env",
])
def test_forbidden_write_targets_are_refused_without_touching_disk(
        engine, project, path):
    ready_agent(engine)
    package = engine.get("worker")
    sandbox = engine.runtime.sandbox(package, actor="alice", approved=True)
    before = sorted(p.name for p in Path(project).rglob("*"))
    outcome = sandbox.use_tool("write_file", path=path, content="x = 1\n")
    assert outcome["allowed"] is False, path
    assert outcome["reason"], path
    after = sorted(p.name for p in Path(project).rglob("*"))
    assert before == after, "a refused write must not change the tree"
    assert sandbox.checkpoint is None, \
        "a refused write must not even take a checkpoint"


def test_documentation_agent_is_confined_to_its_allowed_paths(engine,
                                                              project):
    ready_agent(engine, name="scribe", template="documentation")
    sandbox = engine.runtime.sandbox(engine.get("scribe"), actor="alice",
                                     approved=True)
    allowed = sandbox.use_tool("write_file", path="docs/guide.md",
                               content="# Guide\n")
    assert allowed["allowed"] is True
    assert (Path(project) / "docs" / "guide.md").is_file()
    outside = sandbox.use_tool("write_file", path="app.py",
                               content="x = 1\n")
    assert outside["allowed"] is False
    assert "outside the allowed paths" in outside["reason"]
    assert (Path(project) / "app.py").read_text("utf-8") == \
        "def health():\n    return True\n"


def test_read_only_template_cannot_write_even_when_approved(engine,
                                                            project):
    ready_agent(engine, name="scout", template="research")
    package = engine.get("scout")
    assert "write_file" not in package.spec.tool_names()
    sandbox = engine.runtime.sandbox(package, actor="alice", approved=True)
    outcome = sandbox.use_tool("write_file", path="out.py", content="x = 1\n")
    assert outcome["allowed"] is False
    assert not (Path(project) / "out.py").exists()
    # Its permission view blocks the write operation outright.
    from forge.agents.engine import GrantLedger
    from forge.security.permissions import PermissionLevel

    ledger = GrantLedger(package.name, package.spec,
                         engine.store.read_grants("scout"))
    manager = engine.runtime.agent_manager(package, ledger)
    assert manager.rules["write_file"] == PermissionLevel.BLOCKED


def test_policy_gate_denial_stops_the_write(engine, project):
    """An explicit engine DENY beats an approved run."""
    from forge.security.policy import PermissionPolicy, PermissionRule
    from forge.security.policy import Resource
    from forge.security.policy_gate import PolicyDecision
    from forge.security.permissions import PermissionManager

    policy = PermissionPolicy(rules=(
        PermissionRule(id="block-target", resource=Resource.FILESYSTEM,
                       operation="write", scope="blocked.py",
                       effect=PolicyDecision.DENY,
                       reason="protected by policy"),
    ))
    manager = PermissionManager(policy=policy)
    local = make_engine(project, permission_manager=manager)
    ready_agent(local)
    payload = action_payload(actions=[write_action("blocked.py", "x = 1\n")])
    local.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    outcome = local.run("worker", "write it", actor="alice", approved=True)
    assert outcome["success"] is False or not (
        Path(project) / "blocked.py").exists()
    refused = [action for action in outcome["actions"]
               if action["tool"] == "write_file"]
    assert refused and refused[0]["allowed"] is False
    assert "DENY" in refused[0]["reason"] or "Denied" in refused[0]["reason"]
    assert not (Path(project) / "blocked.py").exists()


def test_unapproved_write_requires_approval_in_assisted_mode(engine,
                                                             project):
    ready_agent(engine)
    payload = action_payload(actions=[write_action("out.py", "x = 1\n")])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    refused = engine.run("worker", "write it", actor="alice", approved=False)
    assert not (Path(project) / "out.py").exists()
    write = [action for action in refused["actions"]
             if action["tool"] == "write_file"][0]
    assert write["allowed"] is False
    assert "Approval required" in write["reason"] or \
        "REQUIRE_APPROVAL" in write["decision"]

    approved_run = engine.run("worker", "write it", actor="alice",
                             approved=True)
    assert approved_run["success"] is True
    assert (Path(project) / "out.py").is_file()


# -- memory isolation ----------------------------------------------------


def test_agent_memory_is_isolated_between_agents(engine, project):
    ready_agent(engine, name="worker-a")
    ready_agent(engine, name="worker-b")
    sandbox_a = engine.runtime.sandbox(engine.get("worker-a"), actor="alice")
    sandbox_b = engine.runtime.sandbox(engine.get("worker-b"), actor="alice")
    sandbox_a.remember("decision", "A chose CSV")
    assert sandbox_a.recall("decision") == "A chose CSV"
    assert sandbox_b.recall("decision") is None
    assert sandbox_a.keys() == ["decision"]
    assert sandbox_b.keys() == []
    with pytest.raises(AgentIsolationError):
        sandbox_b.recall("decision", owner="worker-a")


def test_memory_traversal_is_refused(engine):
    ready_agent(engine, name="worker-a")
    ready_agent(engine, name="worker-b")
    sandbox = engine.runtime.sandbox(engine.get("worker-a"), actor="alice")
    for key in ("../worker-b/decision", "/etc/passwd", "..", "a/../../b"):
        with pytest.raises(ValueError):
            sandbox.remember(key, "x")


def test_memory_scope_none_disables_memory(engine):
    engine.create_from_template(
        "research", name="scout", actor="alice",
        overrides={"memory": {"scope": "none"}})
    engine.validate("scout", actor="alice")
    engine.grant_spec("scout", actor="alice")
    engine.test("scout", actor="alice")
    engine.enable("scout", actor="alice")
    sandbox = engine.runtime.sandbox(engine.get("scout"), actor="alice")
    with pytest.raises(AgentPermissionError):
        sandbox.remember("key", "value")
    with pytest.raises(AgentPermissionError):
        sandbox.recall("key")


def test_project_scope_memory_is_shared_only_with_consent(engine):
    engine.create_from_template("research", name="scout", actor="alice")
    engine.create_from_template(
        "coding", name="worker", actor="alice",
        overrides={"memory": {"scope": "agent"}})
    for name in ("scout", "worker"):
        engine.validate(name, actor="alice")
        engine.grant_spec(name, actor="alice")
        engine.test(name, actor="alice")
        engine.enable(name, actor="alice")
    scout = engine.runtime.sandbox(engine.get("scout"), actor="alice")
    worker = engine.runtime.sandbox(engine.get("worker"), actor="alice")
    scout.remember("finding", "shared fact", shared=True)
    assert scout.recall("finding", shared=True) == "shared fact"
    with pytest.raises(AgentPermissionError):
        worker.recall("finding", shared=True)
    with pytest.raises(AgentPermissionError):
        worker.remember("finding", "nope", shared=True)


def test_memory_limits_are_enforced(engine):
    engine.create_from_template(
        "coding", name="worker", actor="alice",
        overrides={"memory": {"scope": "agent", "max_entries": 2,
                              "max_entry_bytes": 32}})
    engine.validate("worker", actor="alice")
    engine.grant_spec("worker", actor="alice")
    engine.test("worker", actor="alice")
    engine.enable("worker", actor="alice")
    sandbox = engine.runtime.sandbox(engine.get("worker"), actor="alice")
    sandbox.remember("one", "1")
    sandbox.remember("two", "2")
    with pytest.raises(AgentPermissionError):
        sandbox.remember("three", "3")
    with pytest.raises(AgentPermissionError):
        sandbox.remember("one", "x" * 200)


# -- resource limits -----------------------------------------------------


def test_hourly_run_limit_is_enforced(engine):
    engine.create_from_template(
        "coding", name="worker", actor="alice",
        overrides={"limits": {"max_runs_per_hour": 2},
                   "verification": {"require_tests": False}})
    engine.validate("worker", actor="alice")
    engine.grant_spec("worker", actor="alice")
    engine.test("worker", actor="alice")
    engine.enable("worker", actor="alice")
    assert engine.run("worker", "one", actor="alice")["stage"] != "limits"
    assert engine.run("worker", "two", actor="alice")["stage"] != "limits"
    blocked = engine.run("worker", "three", actor="alice")
    assert blocked["success"] is False
    assert blocked["stage"] == "limits"
    assert "hourly run limit" in blocked["error"]


def test_model_call_limit_is_enforced_within_a_run(engine, project):
    engine.create_from_template(
        "coding", name="worker", actor="alice",
        overrides={"limits": {"max_model_calls": 1},
                   "verification": {"require_tests": False}})
    engine.validate("worker", actor="alice")
    engine.grant_spec("worker", actor="alice")
    engine.test("worker", actor="alice")
    engine.enable("worker", actor="alice")
    package = engine.get("worker")
    from forge.agents.engine import RunBudget

    budget = RunBudget(limits=package.spec.limits, started_at=0.0)
    budget.charge_model()
    with pytest.raises(AgentLimitError):
        budget.charge_model()


# -- verification, checkpoints, rollback ---------------------------------


def test_successful_run_writes_verifies_and_records(engine, project):
    ready_agent(engine)
    payload = action_payload(
        summary="added export",
        actions=[write_action("export.py",
                              "def export_csv():\n    return 'x'\n")],
        memory={"decision": "csv"})
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker", approved=True)
    assert result["success"] is True, result
    assert result["stage"] == "complete"
    assert (Path(project) / "export.py").is_file()
    assert [gate["name"] for gate in result["gates"]] == [
        "security", "review", "tests"]
    assert all(gate["passed"] for gate in result["gates"])
    assert result["files_changed"] == ["export.py"]
    assert result["memory"] == [{"key": "decision", "shared": False,
                                 "bytes": 3}]
    assert result["model"] == "m/agent"
    history = engine.history("worker")
    assert history[-1]["success"] is True
    assert engine.get("worker").run_count == 1


def test_verification_failure_rolls_back_the_write(engine, project):
    """A secret in a written file fails the gate and reverts the file."""
    ready_agent(engine)
    payload = action_payload(actions=[write_action(
        "config.py", 'api_key = "abcdefgh12345678"\n')])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker", approved=True)
    assert result["success"] is False
    assert result["stage"] == "verification"
    assert result["rolled_back"] is True
    assert not (Path(project) / "config.py").exists()
    assert any(gate["name"] == "security" and not gate["passed"]
               for gate in result["gates"])
    assert engine.history("worker")[-1]["rolled_back"] is True


def test_dangerous_call_in_written_code_is_refused_and_reverted(engine,
                                                                project):
    ready_agent(engine)
    payload = action_payload(actions=[write_action(
        "runner.py", 'import os\nos.system("rm -rf /")\n')])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker", approved=True)
    assert result["success"] is False
    assert result["rolled_back"] is True
    assert not (Path(project) / "runner.py").exists()


def test_failing_project_tests_fail_the_run_and_roll_back(engine, project):
    ready_agent(engine)
    payload = action_payload(actions=[write_action(
        "app.py", "def health():\n    return False\n")])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker", approved=True)
    assert result["success"] is False
    assert any(gate["name"] == "tests" and not gate["passed"]
               for gate in result["gates"])
    assert (Path(project) / "app.py").read_text("utf-8") == \
        "def health():\n    return True\n", "the checkpoint must restore it"


def test_a_run_with_no_writes_reports_no_write_gates(engine, project):
    ready_agent(engine)
    payload = action_payload(summary="nothing to change")
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker", approved=True)
    assert result["success"] is True
    assert result["gates"] == [], \
        "unexecuted gates must not be reported as passed"
    assert result["output"] == "nothing to change"


def test_a_run_that_never_wrote_does_not_claim_a_rollback(engine, project):
    """The run record must not report a rollback that never happened."""
    ready_agent(engine)
    # Fails at the model stage, before any tool call or write.
    engine.runtime.fabric = None
    result = engine.run("worker", "add export", actor="alice", approved=True)
    assert result["success"] is False
    assert result["stage"] == "model"
    assert result["files_changed"] == []
    assert result["checkpoint"] == ""
    assert result["rolled_back"] is False, \
        "nothing was written, so nothing was rolled back"
    assert engine.history("worker")[-1]["rolled_back"] is False


def test_checkpoint_is_taken_before_the_first_write(engine, project):
    ready_agent(engine)
    package = engine.get("worker")
    sandbox = engine.runtime.sandbox(package, actor="alice", approved=True)
    assert sandbox.checkpoint is None
    sandbox.use_tool("write_file", path="out.py", content="x = 1\n")
    assert sandbox.checkpoint is not None
    assert (Path(project) / "out.py").is_file()
    sandbox.rollback()
    assert not (Path(project) / "out.py").exists()


# -- model honesty -------------------------------------------------------


def test_a_placeholder_response_is_not_accepted_as_model_output(project):
    """With no real model, the run fails instead of inventing success."""
    from forge.models.fabric import ModelFabric
    from forge.models.provider import LocalModelProvider, ProviderRegistry
    from forge.models.registry import Model, ModelRegistry

    engine = make_engine(project)
    ready_agent(engine)  # built against a real (scripted) model
    fallback = ModelFabric(
        registry=ModelRegistry([
            Model(name="local-fallback", provider="local",
                  capabilities=("coding",), fallback=True, free=True,
                  local=True)]),
        providers=ProviderRegistry({"local": LocalModelProvider()}))
    engine.runtime.fabric = fallback
    result = engine.run("worker", "add export", actor="alice", approved=True)
    assert result["success"] is False
    assert "placeholder" in result["error"]
    assert result["actions"] == []


def test_no_fabric_is_an_honest_failure(project):
    from forge.agents.engine import AgentCreationEngine

    engine = AgentCreationEngine(str(project))
    ready_agent(engine)
    result = engine.run("worker", "add export", actor="alice", approved=True)
    assert result["success"] is False
    assert "No Model Fabric" in result["error"]


def test_model_routing_uses_the_spec_requirements(engine, project):
    ready_agent(engine)
    payload = action_payload(summary="ok")
    provider = AgentProvider(payload)
    engine.runtime.fabric = make_agent_fabric(provider)
    result = _run(engine, "worker", approved=True)
    assert result["success"] is True
    assert result["model"] == "m/agent"
    assert provider.prompts, "the fabric must have been called"
    assert "worker" in provider.prompts[0]
    assert "Tools you may request" in provider.prompts[0]


# -- isolation between agents at runtime ---------------------------------


def test_two_agents_do_not_share_state(engine, project):
    ready_agent(engine, name="worker-a")
    ready_agent(engine, name="worker-b")
    payload_a = action_payload(actions=[write_action("a.py", "A = 1\n")],
                               memory={"who": "a"})
    payload_b = action_payload(actions=[write_action("b.py", "B = 1\n")],
                               memory={"who": "b"})
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload_a))
    first = engine.run("worker-a", "write a", actor="alice", approved=True)
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload_b))
    second = engine.run("worker-b", "write b", actor="alice", approved=True)
    assert first["files_changed"] == ["a.py"]
    assert second["files_changed"] == ["b.py"]
    sandbox_a = engine.runtime.sandbox(engine.get("worker-a"), actor="alice")
    sandbox_b = engine.runtime.sandbox(engine.get("worker-b"), actor="alice")
    assert sandbox_a.recall("who") == "a"
    assert sandbox_b.recall("who") == "b"
    assert engine.get("worker-a").run_count == 1
    assert engine.get("worker-b").run_count == 1


def test_run_history_is_bounded_and_per_agent(engine):
    ready_agent(engine, name="worker-a")
    payload = action_payload(summary="noop")
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    for _ in range(3):
        engine.run("worker-a", "noop", actor="alice")
    assert len(engine.history("worker-a")) == 3
    assert all(entry["success"] is True
               for entry in engine.history("worker-a"))
    assert engine.get("worker-a").run_count == 3


def test_malformed_model_output_is_never_executed(engine, project):
    ready_agent(engine)

    class Garbage:
        name = "garbage"

        def generate(self, prompt, **kwargs):
            from forge.models.provider import ModelResult

            return ModelResult("here you go: {not json at all", self.name)

    engine.runtime.fabric = make_agent_fabric(Garbage())
    result = _run(engine, "worker", approved=True)
    assert result["actions"] == []
    assert result["files_changed"] == []
    assert list(Path(project).glob("*.py")) == [Path(project) / "app.py"]


def test_model_cannot_inject_dangerous_tool_arguments(engine, project):
    ready_agent(engine)
    payload = json.dumps({
        "summary": "sneaky",
        "actions": [{"tool": "write_file",
                     "args": {"path": "ok.py", "content": "x = 1\n",
                              "__import__": "os",
                              "content; import os": "bad",
                              "nested": {"a": 1}}}],
    })
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker", approved=True)
    write = [action for action in result["actions"]
             if action["tool"] == "write_file"][0]
    assert write["allowed"] is True
    assert (Path(project) / "ok.py").read_text("utf-8") == "x = 1\n"
    assert not list(Path(project).glob("os*"))
