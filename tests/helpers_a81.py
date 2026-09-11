"""Shared fixtures for the A81 Agent Creation Engine tests."""
from __future__ import annotations

import subprocess
from pathlib import Path

from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry


def make_repo(root: Path) -> Path:
    """A tiny, real git project the engine can work over."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("def health(): return True\n")
    (root / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_app.py").write_text(
        "from app import health\n"
        "def test_health():\n    assert health()\n")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "--", ".gitignore", "app.py",
                    "tests/test_app.py"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True,
                   capture_output=True)
    return root


def _payload(changes: list[dict]) -> str:
    import json

    return json.dumps({"changes": changes})


GOOD_CHANGES = _payload([
    {"path": "notes.py", "action": "create",
     "content": "def note(): return 'x'\n"},
])

DANGEROUS_CHANGES = _payload([
    {"path": "evil.py", "action": "create",
     "content": "import os\nos.system('rm -rf /tmp/x')\n"},
])


class ScriptedAgentProvider:
    """Deterministic provider that answers with one scripted payload."""

    name = "scripted"

    def __init__(self, payload: str = GOOD_CHANGES) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def generate(self, prompt: str, **kwargs) -> ModelResult:
        self.prompts.append(prompt)
        return ModelResult(self.payload, self.name)


def make_fabric(payload: str = GOOD_CHANGES,
                capabilities: tuple = ("coding", "review", "security",
                                       "documentation", "research"),
                context_window: int = 8192) -> tuple:
    """A fabric whose scripted provider always answers ``payload``."""
    if not isinstance(payload, str):
        raise TypeError("make_fabric(payload) wants the payload STRING, "
                        "not a provider")
    provider = ScriptedAgentProvider(payload)
    registry = ModelRegistry()
    registry.register(Model(
        name="scripted/agent", provider="scripted",
        capabilities=capabilities, context_window=context_window,
        free=True, local=True))
    providers = ProviderRegistry()
    providers.register("scripted", provider)
    return ModelFabric(registry=registry, providers=providers), provider


def make_engine(root: Path, payload: str = GOOD_CHANGES, *,
                mode: str = "autonomous"):
    """Factory + runtime over a real repo, with a scripted fabric."""
    from forge.agents.engine import AgentCreationFactory, EngineBundle, EngineRuntime

    fabric, provider = make_fabric(payload)
    factory = AgentCreationFactory(root=str(root))
    bundle = EngineBundle.build(root, fabric=fabric, mode=mode)
    runtime = EngineRuntime(bundle)
    return {"factory": factory, "bundle": bundle, "runtime": runtime,
            "fabric": fabric, "provider": provider}


def ready_agent(engine: dict, template: str = "coding", name: str = "worker",
                actor: str = "alice") -> "object":
    """Create + validate + test + enable an agent through its lifecycle."""
    from forge.agents.engine import run_agent_benchmark, spec_from_template

    factory = engine["factory"]
    spec = spec_from_template(template, name=name)
    package = factory.create(spec, created_by=actor)
    package = factory.validate(name, actor=actor)
    benchmark = run_agent_benchmark(package, engine["runtime"], factory)
    assert benchmark["passed"], benchmark
    package = factory.mark_tested(name, actor=actor, benchmark=benchmark)
    package = factory.enable(name, actor=actor)
    return factory.get(name)
