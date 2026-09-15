"""Multi-agent engineering system (A83).

Fourteen specialists, one shared :class:`ProjectContext`, and a pipeline that
derives its execution order from what each agent declares it reads and writes
instead of a hard-coded sequence.
"""
from __future__ import annotations

from forge.engineering.context import (
    AgentTurn,
    ContextError,
    Fact,
    ProjectContext,
)
from forge.engineering.pipeline import (
    EngineeringPipeline,
    PipelineReport,
    StageResult,
    order_specs,
)
from forge.engineering.roster import (
    AgentSpec,
    ContextAgent,
    Roster,
    ROLES,
    default_roster,
    role_for_capability,
)

__all__ = [
    "AgentSpec", "AgentTurn", "ContextAgent", "ContextError",
    "EngineeringPipeline", "Fact", "PipelineReport", "ProjectContext",
    "ROLES", "Roster", "StageResult", "default_roster", "order_specs",
    "role_for_capability",
]
