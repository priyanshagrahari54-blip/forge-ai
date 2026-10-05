"""High-level requirement planner (A02/A06 task decomposition).

Produces a deterministic, ordered plan for a requirement. Agent-level planning
(capability → agent selection) is handled by ``forge.agents.planner``; this
planner turns a raw requirement into validated, executable steps.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlanStep:
    id: str
    description: str
    depends_on: tuple[str, ...] = ()


@dataclass
class Plan:
    steps: tuple[PlanStep, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {"steps": [{"id": s.id, "description": s.description, "depends_on": list(s.depends_on)} for s in self.steps]}


#: Canonical plan shape, in dependency order. Deterministic and executable.
_PLAN_TEMPLATE: tuple[tuple[str, str], ...] = (
    ("requirements", "Understand and cross-check the requirements: {request}"),
    ("architecture", "Define architecture, components, dependencies, and acceptance criteria."),
    ("capabilities", "Resolve reusable capabilities, models, tools, and project profiles."),
    ("execution", "Implement the required changes through the task graph."),
    ("verification", "Run tests, security checks, benchmarks, and final quality verification."),
)


class Planner:
    def create_plan(self, request: str, *, memory=None,
                    project: str | None = None, root: str | None = None) -> list[PlanStep]:
        """Build the canonical plan, optionally enriched with memory recall.

        ``memory``/``project`` are optional long-term-memory integration
        points: when supplied, relevant remembered knowledge is recalled and
        surfaced as the first step of the plan so the executor begins from
        what Forge already knows. With ``memory`` omitted the plan is
        byte-identical to the pre-memory behavior.
        """
        if not request or not request.strip():
            raise ValueError("Cannot plan an empty requirement")
        steps: list[PlanStep] = []
        previous_id: str | None = None

        if memory is not None:
            from forge.memory.integrations import (
                memory_context_text,
                recall_for_planning,
            )

            recalled = recall_for_planning(
                memory, project or "default", request)
            context = memory_context_text(recalled)
            if context:
                steps.append(PlanStep(
                    id="0",
                    description="Recall relevant project memory: " + context,
                ))
                previous_id = "0"

        for index, (phase, description) in enumerate(_PLAN_TEMPLATE, start=1):
            step_id = str(index)
            steps.append(PlanStep(
                id=step_id,
                description=description.format(request=request),
                depends_on=(previous_id,) if previous_id else (),
            ))
            previous_id = step_id
        return steps
