"""Tool Intelligence (A84 Stage E).

Forge is already tool-*capable* (files, git, terminal, browser, network,
vision, voice, compute, research, models — each behind the A33 policy). This
package makes it tool-*aware*: a declarative capability registry the planner
can reason over, a planner that picks the tool the task actually requires,
and a post-execution verifier that validates, flags staleness and decides
whether a retry or a cross-check is safe.

Submodules:

* :mod:`registry` — ``ToolDescriptor`` + ``ToolCapabilityRegistry`` (E1).
* :mod:`planner` — capability-driven tool planning (E2). Planning only;
  execution remains on the permissioned paths.
* :mod:`verification` — result validation, staleness and safe retry policy
  (E3).

Nothing here grants authority: a registered tool is a *description*, not a
permission. Running anything still evaluates the A33 policy for the mapped
resource/operation, and Forge never treats "registered" as "usable" or
"called" as "succeeded" (Stage R).
"""
from forge.tools.intelligence.registry import (
    ToolCapabilityRegistry,
    ToolDescriptor,
    ToolRisk,
    builtin_registry,
)
from forge.tools.intelligence.planner import ToolPlan, ToolPlanner
from forge.tools.intelligence.verification import ToolVerdict, ToolVerifier

__all__ = [
    "ToolCapabilityRegistry",
    "ToolDescriptor",
    "ToolPlan",
    "ToolPlanner",
    "ToolRisk",
    "ToolVerifier",
    "builtin_registry",
]
