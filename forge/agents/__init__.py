"""Agent fleet primitives.

The canonical specialist fleet lives in :mod:`forge.agents.frontier_fleet`
(40 families x 26 variants = 1,040 logical roles that resolve their eligible
models from the live Model Fabric registry at request time).
"""

from forge.agents.frontier_fleet import (
    FLEET_LABEL,
    SPECIALIZATIONS,
    FrontierModelAgentExecutor,
    build_frontier_fleet,
    eligible_models,
    extend_registry_with_frontier_fleet,
)

__all__ = [
    "FLEET_LABEL",
    "SPECIALIZATIONS",
    "FrontierModelAgentExecutor",
    "build_frontier_fleet",
    "eligible_models",
    "extend_registry_with_frontier_fleet",
]
