"""Forge Agent Creation Engine (A82).

First-party engine for creating specialized software agents from
structured specifications. An agent is a validated specification, a
versioned on-disk package, an explicit lifecycle, and a runtime that can
only act through the Model Fabric, the PolicyGate, the Tool Runtime,
policy-bounded Memory, real Verification, and exact Checkpoints.

Nothing here lets an agent widen its own permissions: grants are recorded
by a named operator, bounded by the specification, and re-checked by the
PolicyGate on every single call.
"""
from forge.agents.engine.benchmark import (
    REQUIRED_SCENARIOS,
    BenchmarkReport,
    ScenarioResult,
    run_agent_benchmark,
)
from forge.agents.engine.core import AgentCreationEngine
from forge.agents.engine.errors import (
    AgentBenchmarkError,
    AgentEngineError,
    AgentExistsError,
    AgentIsolationError,
    AgentLifecycleError,
    AgentLimitError,
    AgentNotFoundError,
    AgentPackageError,
    AgentPermissionError,
    AgentSpecError,
    AgentVersionError,
)
from forge.agents.engine.factory import AgentFactory
from forge.agents.engine.governor import AgentGovernor, RunBudget
from forge.agents.engine.grants import Grant, GrantLedger
from forge.agents.engine.lifecycle import (
    TRANSITIONS,
    AgentState,
    LifecycleRecord,
    Transition,
)
from forge.agents.engine.memory import AgentMemory
from forge.agents.engine.package import AgentPackage, PackageStore
from forge.agents.engine.runtime import (
    AgentRunResult,
    AgentRuntime,
    AgentSandbox,
    lifecycle_refusal,
    strict_mode_for,
    stricter_mode,
)
from forge.agents.engine.spec import (
    FORBIDDEN_OPERATIONS,
    GRANTABLE_OPERATIONS,
    TOOL_CATALOG,
    AgentSpec,
    MemoryPolicy,
    ModelRequirements,
    PermissionSpec,
    ResourceLimits,
    ToolGrant,
    VerificationSpec,
    validate_spec,
)
from forge.agents.engine.templates import (
    TEMPLATES,
    describe_templates,
    spec_from_template,
    template_ids,
)
from forge.agents.engine.versioning import (
    bump,
    change_kind,
    diff_specs,
    next_version,
)

__all__ = [
    "AgentCreationEngine",
    "AgentFactory",
    "AgentRuntime",
    "AgentSandbox",
    "AgentRunResult",
    "AgentSpec",
    "ToolGrant",
    "PermissionSpec",
    "ModelRequirements",
    "MemoryPolicy",
    "VerificationSpec",
    "ResourceLimits",
    "validate_spec",
    "TOOL_CATALOG",
    "GRANTABLE_OPERATIONS",
    "FORBIDDEN_OPERATIONS",
    "AgentState",
    "LifecycleRecord",
    "Transition",
    "TRANSITIONS",
    "lifecycle_refusal",
    "AgentPackage",
    "PackageStore",
    "GrantLedger",
    "Grant",
    "AgentMemory",
    "AgentGovernor",
    "RunBudget",
    "BenchmarkReport",
    "ScenarioResult",
    "run_agent_benchmark",
    "REQUIRED_SCENARIOS",
    "TEMPLATES",
    "template_ids",
    "describe_templates",
    "spec_from_template",
    "bump",
    "change_kind",
    "diff_specs",
    "next_version",
    "strict_mode_for",
    "stricter_mode",
    "AgentEngineError",
    "AgentSpecError",
    "AgentNotFoundError",
    "AgentExistsError",
    "AgentLifecycleError",
    "AgentPermissionError",
    "AgentIsolationError",
    "AgentLimitError",
    "AgentVersionError",
    "AgentPackageError",
    "AgentBenchmarkError",
]
