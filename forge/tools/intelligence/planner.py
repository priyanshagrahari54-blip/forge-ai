"""Tool planning (A84 Stage E2).

The planner answers one question: **which tool is needed?** — instead of
calling tools randomly. Input is the required-capability signal (usually from
the prompt-intelligence pipeline) plus optional task facts; output is an
ordered, *bounded* tool plan where every step names the A33 gate that must
pass before it can execute.

The planner is advisory. It cannot authorize, escalate or bypass anything:

* steps carry the permission mapping so executors consult policy at the real
  boundary — the plan itself is not a permission;
* unavailable tools (probe says blocked/missing/configured-only) are dropped
  with a recorded reason, not substituted by a faked execution;
* HIGH/CRITICAL tools are flagged `requires_confirmation` so the assistant
  behavior layer asks the user first (Stage Q);
* a task with no tool requirement produces an empty plan — *no tool*, not a
  decorative one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["ToolPlan", "ToolPlanner"]

MAX_STEPS = 6

#: Capability families that imply tools at all (conversational answers do not).
TOOL_NEUTRAL_CAPABILITIES = frozenset({"reasoning", "documentation"})


@dataclass(frozen=True)
class ToolPlanStep:
    tool: str
    reason: str
    permission: Dict[str, str]
    risk: str
    requires_confirmation: bool
    availability: str = "architecture"
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "reason": self.reason,
            "permission": dict(self.permission),
            "risk": self.risk,
            "requires_confirmation": self.requires_confirmation,
            "availability": self.availability,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ToolPlan:
    steps: Tuple[ToolPlanStep, ...] = ()
    skipped: Tuple[Dict[str, str], ...] = ()
    #: True when the request may be answered without any tool at all.
    direct_answer_ok: bool = False
    coverage: Tuple[str, ...] = ()      # capabilities actually served
    uncovered: Tuple[str, ...] = ()     # capabilities no tool serves

    @property
    def empty(self) -> bool:
        return not self.steps

    def to_dict(self) -> Dict[str, Any]:
        return {
            "steps": [step.to_dict() for step in self.steps],
            "skipped": [dict(item) for item in self.skipped],
            "direct_answer_ok": self.direct_answer_ok,
            "coverage": list(self.coverage),
            "uncovered": list(self.uncovered),
            "honesty": "a plan is not a permission; every step is checked "
                       "against policy at execution time",
        }


class ToolPlanner:
    """Select tools from capability requirements, deterministically."""

    def __init__(self, registry: Any, *,
                 allowed_states: Sequence[str] = ("live", "ready",
                                                  "architecture",
                                                  "simulated", "configured")) -> None:
        self.registry = registry
        #: States a tool must be in to appear in a plan. ``blocked``/
        #: ``missing``/``error`` never plan; ``configured``/``architecture``
        #: /``simulated`` may, so long as execution still passes policy.
        self.allowed_states = tuple(allowed_states)

    def plan_for_capabilities(self, capabilities: Iterable[str], *,
                              context: Optional[Dict[str, Any]] = None) -> ToolPlan:
        wanted = tuple(dict.fromkeys(str(c) for c in capabilities if c))
        if not wanted or set(wanted) <= TOOL_NEUTRAL_CAPABILITIES:
            return ToolPlan(direct_answer_ok=not wanted,
                            uncovered=tuple(c for c in wanted
                                            if c not in TOOL_NEUTRAL_CAPABILITIES))
        steps: List[ToolPlanStep] = []
        skipped: List[Dict[str, str]] = []
        covered: List[str] = []
        seen_tools: set = set()
        for capability in wanted:
            candidates = self.registry.by_capability(capability)
            matched = False
            for tool in sorted(candidates,
                               key=lambda t: (t.risk_rank, t.name)):
                if tool.name in seen_tools:
                    matched = True
                    continue
                availability = self.registry.availability(tool.name)
                state = str(availability.get("state", "unknown"))
                if state not in self.allowed_states:
                    skipped.append({"tool": tool.name,
                                    "reason": f"availability={state}"})
                    continue
                seen_tools.add(tool.name)
                steps.append(ToolPlanStep(
                    tool=tool.name,
                    reason=f"required capability: {capability}",
                    permission={"resource": tool.permission_resource,
                                "operation": tool.permission_operation},
                    risk=tool.risk,
                    requires_confirmation=tool.requires_approval_hint(),
                    availability=state,
                    notes=_step_notes(tool, context)))
                covered.append(capability)
                matched = True
                break
            if not matched:
                skipped.append({"tool": "-", "reason":
                                f"no registered tool serves {capability!r}"})
        uncovered = tuple(c for c in wanted if c not in covered)
        return ToolPlan(steps=tuple(steps[:MAX_STEPS]),
                        skipped=tuple(skipped),
                        direct_answer_ok=bool(context and context.get(
                            "answerable_without_tools")),
                        coverage=tuple(covered), uncovered=uncovered)

    def plan_for_request(self, enhanced: Any, *,
                         context: Optional[Dict[str, Any]] = None) -> ToolPlan:
        """Convenience: consume a pipeline ``EnhancedPrompt``."""
        capabilities = tuple(getattr(enhanced, "capabilities", ()) or ())
        return self.plan_for_capabilities(capabilities, context=context)


def _step_notes(tool: Any, context: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    notes: List[str] = []
    if tool.auth_required:
        notes.append("provider authentication required")
    if tool.staleness_bound:
        notes.append("results carry a retrieval timestamp")
    if tool.idempotent:
        notes.append("retry-safe (idempotent)")
    if tool.risk == "CRITICAL":
        notes.append("production-mutating: explicit operator confirmation "
                     "is mandatory")
    return tuple(notes)
