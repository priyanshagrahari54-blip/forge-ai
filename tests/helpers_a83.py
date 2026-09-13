"""Shared fixtures for the A83 Agent Creation Engine suites.

Everything here is deterministic and offline: a scripted provider stands
in for a model so the *whole* engine path (fabric routing, sandbox,
PolicyGate, verification, checkpoints, memory, history) really executes.
Where a suite needs to prove the engine refuses to fake success, it uses
a fabric with no real model instead.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from forge.agents.engine import AgentCreationEngine
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry

#: Capabilities the test model advertises, covering every template.
CAPABILITIES = (
    "coding", "reasoning", "planning", "debugging", "testing", "review",
    "security", "research", "documentation", "long_context",
    "structured_output",
)


def action_payload(summary: str = "done", actions=None, memory=None) -> str:
    """A structured agent response in the engine's JSON protocol."""
    return json.dumps({
        "summary": summary,
        "actions": list(actions or []),
        "memory": dict(memory or {}),
    })


def write_action(path: str, content: str) -> dict:
    return {"tool": "write_file", "args": {"path": path, "content": content}}


class AgentProvider:
    """Deterministic provider returning one scripted payload."""

    name = "agent-scripted"

    def __init__(self, payload: str = "") -> None:
        self.payload = payload or action_payload()
        self.prompts: list = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return ModelResult(self.payload, self.name)


class FailingProvider:
    """Provider that reports a routing failure (never fabricates output)."""

    name = "agent-failing"

    def generate(self, prompt, **kwargs):
        return ModelResult("", self.name, error="provider unavailable")


def make_agent_fabric(provider=None) -> ModelFabric:
    """A fabric with one real (non-fallback) model behind ``provider``."""
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/agent", provider="p", capabilities=CAPABILITIES,
                  context_window=32768, free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider or AgentProvider()}),
    )


def make_project(root: Path, *, with_tests: bool = True) -> Path:
    """A tiny real project the agents can actually work on."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("def health():\n    return True\n",
                                 encoding="utf-8")
    if with_tests:
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "test_app.py").write_text(
            "from app import health\n\n\n"
            "def test_health():\n    assert health() is True\n",
            encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root,
                   check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=root, check=True)
    return root


def make_engine(root, provider=None, *, fabric=None, **kwargs
                ) -> AgentCreationEngine:
    """An engine over ``root`` with a real scripted model available."""
    if fabric is None:
        fabric = make_agent_fabric(provider)
    return AgentCreationEngine(str(root), fabric=fabric, **kwargs)


def ready_agent(engine: AgentCreationEngine, name: str = "worker",
                template: str = "coding", actor: str = "alice",
                *, purpose: str = "", grant: bool = True) -> dict:
    """Drive one agent through create → validate → grant → test → enable."""
    engine.create_from_template(template, name, actor=actor, purpose=purpose)
    report = engine.validate(name, actor=actor)
    assert report["passed"], report
    if grant:
        engine.grant_spec(name, actor=actor)
    benchmark = engine.test(name, actor=actor)
    assert benchmark["passed"], benchmark
    return engine.enable(name, actor=actor)
