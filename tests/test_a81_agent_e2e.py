"""A81 end-to-end: a created agent works through the real subsystems.

The whole path is exercised: template -> spec -> package -> validation
-> benchmark -> operator enable -> bind -> Model Fabric call -> policy
gate -> tool runtime write -> memory -> verification -> checkpoint
rollback. A deterministic provider stands in for a model; every gate,
tool, and checkpoint is real.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge.agent_engine.engine import AgentCreationEngine
from forge.agent_engine.runtime import AgentRuntimeError
from forge.agent_engine.spec import AgentSpec
from forge.models.capabilities import ALL_CAPABILITIES
from forge.models.fabric import ModelFabric
from forge.models.provider import ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelResponse
from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy_gate import PolicyGate
from forge.security.verification import VerificationPipeline
from forge.tools.checkpoint import CheckpointManager

SPEC = {
    "name": "e2e-agent",
    "purpose": "Add a documented helper inside its own scope.",
    "capabilities": ["coding", "documentation"],
    "tools": ["read_file", "write_file", "search"],
    "permissions": {"read_paths": ["**"],
                    "write_paths": ["docs/**", "src/**"],
                    "require_approval_for_writes": True},
    "model_requirements": {"capability": "coding"},
    "memory_policy": {"scope": "session"},
    "verification": {"required_gates": ["security"]},
    "resource_limits": {"max_files_touched": 5},
}


class DeterministicProvider:
    name = "deterministic"

    def __init__(self) -> None:
        self.prompts = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(str(prompt))
        return ModelResponse(text="def helper():\n    return 1\n",
                             model="m/e2e", provider="p",
                             input_tokens=20, output_tokens=8)

    def is_available(self) -> bool:
        return True


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "app.py").write_text("def health():\n    return True\n")
    (root / "docs" / "index.md").write_text("# Docs\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.fixture()
def wiring(repo):
    provider = DeterministicProvider()
    fabric = ModelFabric(
        registry=ModelRegistry([
            Model(name="m/e2e", provider="p",
                  capabilities=tuple(ALL_CAPABILITIES),
                  free=True, local=True)]),
        providers=ProviderRegistry({"p": provider}))
    manager = PermissionManager(mode=OperationMode.AUTONOMOUS)
    engine = AgentCreationEngine(
        fabric=fabric,
        policy_gate=PolicyGate(manager),
        tool_runtime=create_default_runtime(manager, str(repo)),
        verification=VerificationPipeline(repo),
        checkpoints=CheckpointManager(repo))
    return engine, provider, repo


def test_full_creation_to_execution_path(wiring):
    engine, provider, repo = wiring

    package = engine.create(AgentSpec.from_dict(SPEC))
    assert package.state == "created"

    assert engine.validate(package.name)["valid"]
    report = engine.test(package.name)
    assert report["passed"], report["failures"]
    assert engine.enable(package.name)["state"] == "enabled"

    agent = engine.bind(package.name)
    run = agent.begin_run()
    assert run.checkpoint_id

    # 1. Model Fabric
    response = agent.think("Write a helper function.", run=run)
    assert response.success
    assert response.model == "m/e2e"
    assert "You are e2e-agent" in provider.prompts[-1]
    assert run.tokens == 28

    # 2. Memory
    agent.memory.save("plan", "write src/helper.py")
    assert agent.memory.load("plan") == "write src/helper.py"

    # 3. PolicyGate + Tool Runtime
    result = agent.use_tool("write_file", path="src/helper.py",
                            approved=True, content=response.text, run=run)
    assert result.success, result.error
    assert (repo / "src" / "helper.py").read_text() == response.text

    # 4. Verification
    verdict = agent.verify(["src/helper.py"])
    assert verdict["passed"], verdict
    assert "security" in verdict["gates"]

    summary = agent.end_run(run, "succeeded")
    assert summary["files_touched"] == ["src/helper.py"]
    assert summary["status"] == "succeeded"
    assert summary["denials"] == []


def test_out_of_scope_write_is_denied_end_to_end(wiring):
    engine, _provider, repo = wiring
    package = engine.create(AgentSpec.from_dict(SPEC))
    engine.validate(package.name)
    engine.test(package.name)
    engine.enable(package.name)
    agent = engine.bind(package.name)
    run = agent.begin_run()

    with pytest.raises(AgentRuntimeError):
        agent.use_tool("write_file", path="pyproject.toml", approved=True,
                       content="broken", run=run)
    assert not (repo / "pyproject.toml").exists()
    assert run.denials


def test_failed_run_rolls_back_exactly(wiring):
    engine, _provider, repo = wiring
    package = engine.create(AgentSpec.from_dict(SPEC))
    engine.validate(package.name)
    engine.test(package.name)
    engine.enable(package.name)
    agent = engine.bind(package.name)

    original = (repo / "docs" / "index.md").read_text()
    run = agent.begin_run()
    agent.use_tool("write_file", path="docs/index.md", approved=True,
                   content="# Broken\n", run=run)
    assert (repo / "docs" / "index.md").read_text() == "# Broken\n"
    assert agent.rollback(run)
    assert (repo / "docs" / "index.md").read_text() == original
    assert (repo / "src" / "app.py").exists()
    engine.pause(package.name)
    assert engine.get(package.name).state == "paused"


def test_a_paused_agent_stops_working_immediately(wiring):
    engine, _provider, _repo = wiring
    package = engine.create(AgentSpec.from_dict(SPEC))
    engine.validate(package.name)
    engine.test(package.name)
    engine.enable(package.name)
    agent = engine.bind(package.name)
    agent.begin_run()
    engine.pause(package.name)
    with pytest.raises(AgentRuntimeError):
        agent.think("anything")
    with pytest.raises(AgentRuntimeError):
        agent.use_tool("write_file", path="src/x.py", approved=True,
                       content="x")


def test_prompts_and_context_are_redacted(wiring):
    engine, provider, _repo = wiring
    package = engine.create(AgentSpec.from_dict(SPEC))
    engine.validate(package.name)
    engine.test(package.name)
    engine.enable(package.name)
    agent = engine.bind(package.name)
    agent.begin_run()
    agent.think("use api_key = sk-abcdefghij0123456789 please",
                context="password: hunter2")
    sent = provider.prompts[-1]
    assert "sk-abcdefghij0123456789" not in sent
    assert "[redacted]" in sent


def test_engine_persists_and_reloads_the_whole_fleet(wiring, tmp_path):
    engine, _provider, _repo = wiring
    engine.create(AgentSpec.from_dict(SPEC))
    engine.validate("e2e-agent")
    engine.test("e2e-agent")
    engine.enable("e2e-agent")
    path = engine.save(str(tmp_path / "fleet.json"))
    assert Path(path).exists()

    fresh = AgentCreationEngine()
    assert fresh.load(path) == ["e2e-agent"]
    # Imports always arrive untrusted: never enabled.
    assert fresh.get("e2e-agent").state == "created"
    assert not fresh.lifecycle.can_run("e2e-agent")
