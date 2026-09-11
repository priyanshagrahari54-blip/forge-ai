"""Forge Agent Creation Engine (A81).

Create specialized software agents from structured specifications.
Every created agent operates through the Model Fabric, the PolicyGate,
the Tool Runtime, Memory, Verification, and Checkpoints — and no agent
may ever grant itself a permission.
"""
from __future__ import annotations

from forge.agent_engine.benchmark import AgentBenchmark, BenchmarkReport
from forge.agent_engine.engine import AgentCreationEngine, EngineError
from forge.agent_engine.factory import AgentFactoryEngine
from forge.agent_engine.lifecycle import (
    CREATED,
    DISABLED,
    ENABLED,
    PAUSED,
    RETIRED,
    STATES,
    TESTED,
    VALIDATED,
    LifecycleError,
    LifecycleManager,
)
from forge.agent_engine.package import AgentPackage
from forge.agent_engine.runtime import AgentRuntimeError, BoundAgent
from forge.agent_engine.spec import (
    AgentSpec,
    MemoryPolicy,
    ModelRequirements,
    PermissionSpec,
    ResourceLimits,
    SpecError,
    VerificationRequirements,
)
from forge.agent_engine.templates import (
    TEMPLATE_NAMES,
    TEMPLATES,
    from_template,
)
from forge.agent_engine.validator import PackageValidator, ValidationResult
from forge.agent_engine.version import AgentVersion, classify_change

__all__ = [
    "AgentBenchmark", "BenchmarkReport", "AgentCreationEngine",
    "EngineError", "AgentFactoryEngine", "AgentPackage", "AgentSpec",
    "AgentRuntimeError", "BoundAgent", "MemoryPolicy",
    "ModelRequirements", "PermissionSpec", "ResourceLimits", "SpecError",
    "VerificationRequirements", "TEMPLATE_NAMES", "TEMPLATES",
    "from_template", "PackageValidator", "ValidationResult",
    "AgentVersion", "classify_change", "LifecycleManager",
    "LifecycleError", "STATES", "CREATED", "VALIDATED", "TESTED",
    "ENABLED", "PAUSED", "DISABLED", "RETIRED",
]
