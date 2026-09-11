"""Forge Agent Creation Engine (A81).

First-party engine for creating specialized software agents from
structured specifications:

- :mod:`forge.agent_engine.spec` — the validated agent specification
  (name, purpose, capabilities, tools, permissions, model requirements,
  memory policy, verification requirements, resource limits).
- :mod:`forge.agent_engine.factory` — generates structured, versioned
  agent packages from specifications (no self-granted permissions).
- :mod:`forge.agent_engine.lifecycle` — the lifecycle state machine
  (created/validated/tested/enabled/paused/disabled/retired).
- :mod:`forge.agent_engine.store` — durable, immutable, versioned
  package storage.
- :mod:`forge.agent_engine.runtime` — the agent's entire execution
  surface: Model Fabric, PolicyGate, Tool Runtime, Memory, Verification,
  Checkpoints — and nothing else.
- :mod:`forge.agent_engine.benchmarks` — deterministic, offline,
  code-judged agent benchmark testing.
- :mod:`forge.agent_engine.templates` — built-in templates (coding,
  research, security, game-dev, os-dev, documentation).
- :mod:`forge.agent_engine.manager` — the lifecycle authority
  (the only code that may transition or enable agents).
"""
from __future__ import annotations

from forge.agent_engine.benchmarks import (
    BENCHMARK_IDS,
    BenchmarkResult,
    run_benchmark,
)
from forge.agent_engine.errors import (
    AgentEngineError,
    AgentLimitError,
    AgentNotFoundError,
    NotRunnableError,
    PermissionEscalationError,
    SpecError,
)
from forge.agent_engine.factory import AgentFactory
from forge.agent_engine.lifecycle import AgentLifecycle
from forge.agent_engine.manager import AgentManager
from forge.agent_engine.runtime import AgentRuntime
from forge.agent_engine.spec import (
    AgentSpec,
    MemoryPolicy,
    ModelRequirements,
    ResourceLimits,
    VerificationRequirements,
)
from forge.agent_engine.store import AgentManifest, AgentStore
from forge.agent_engine.templates import TEMPLATE_IDS

__all__ = [
    "AgentEngineError",
    "AgentFactory",
    "AgentLifecycle",
    "AgentLimitError",
    "AgentManager",
    "AgentManifest",
    "AgentNotFoundError",
    "AgentRuntime",
    "AgentSpec",
    "AgentStore",
    "BENCHMARK_IDS",
    "BenchmarkResult",
    "MemoryPolicy",
    "ModelRequirements",
    "NotRunnableError",
    "PermissionEscalationError",
    "ResourceLimits",
    "SpecError",
    "TEMPLATE_IDS",
    "VerificationRequirements",
    "run_benchmark",
]
