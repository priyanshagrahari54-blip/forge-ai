"""Forge Agent Creation Engine (A81).

First-party engine that creates specialized software agents from
structured specifications. A specification enters the
:class:`AgentCreationFactory`, which emits a structured, versioned
:class:`AgentPackage`; the package walks the lifecycle
(``created → validated → tested → enabled ⇄ paused → disabled →
retired``) and, once enabled, runs strictly through the six subsystems
every Forge agent shares:

* Model Fabric — all model access;
* PolicyGate — every write, tool, and permission decision;
* Tool Runtime — permissioned tool execution;
* Memory — namespaced, policy-bounded memory;
* Verification — spec-configured acceptance gates;
* Checkpoints — exact rollback of agent-written files.

No agent may self-grant: specifications are operator-authored and
frozen, agent actors are refused on every mutating path, and the
permission ceiling can only be tightened — never expanded — at
runtime.
"""
from __future__ import annotations

from forge.agents.engine.benchmark import run_agent_benchmark
from forge.agents.engine.factory import (
    AgentCreationFactory,
    EngineGuard,
)
from forge.agents.engine.lifecycle import (
    LifecycleError,
    LifecycleState,
    transition,
)
from forge.agents.engine.package import AgentPackage
from forge.agents.engine.runtime import (
    EngineBundle,
    EngineRunReport,
    EngineRuntime,
)
from forge.agents.engine.spec import (
    AgentSpecification,
    MemoryPolicy,
    ModelRequirements,
    ResourceLimits,
    VerificationRequirements,
)
from forge.agents.engine.store import PackageStore, PackageStoreError
from forge.agents.engine.templates import (
    TEMPLATE_NAMES,
    get_template,
    spec_from_template,
    template_names,
    template_summaries,
)

__all__ = [
    "AgentCreationFactory",
    "AgentPackage",
    "AgentSpecification",
    "EngineBundle",
    "EngineGuard",
    "EngineRunReport",
    "EngineRuntime",
    "LifecycleError",
    "LifecycleState",
    "MemoryPolicy",
    "ModelRequirements",
    "PackageStore",
    "PackageStoreError",
    "ResourceLimits",
    "TEMPLATE_NAMES",
    "VerificationRequirements",
    "get_template",
    "run_agent_benchmark",
    "spec_from_template",
    "template_names",
    "template_summaries",
    "transition",
]
