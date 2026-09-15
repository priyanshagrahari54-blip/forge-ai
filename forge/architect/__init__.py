"""Project Architect (A83): requirement → reviewable, editable plan.

``"Build X"`` becomes specifications, architecture, components, dependencies,
an implementation plan, a test plan, a benchmark plan, and a release plan —
all of it editable, and none of it executable until it is approved.
"""
from __future__ import annotations

from forge.architect.builder import ArchitectError, ProjectArchitect
from forge.architect.classifier import (
    Classification,
    acceptance_criteria_for,
    classify,
    constraints_from,
    goals_from,
)
from forge.architect.models import (
    ArchitectureDecision,
    BenchmarkPlan,
    Component,
    Dependency,
    PlanStatus,
    PlanTask,
    ProjectKind,
    ProjectPlan,
    ReleasePlan,
    Requirement,
    Specification,
    TestPlan,
)
from forge.architect.store import (
    Approval,
    NotApproved,
    PlanError,
    PlanNotFound,
    PlanStore,
)

__all__ = [
    "Approval", "ArchitectError", "ArchitectureDecision", "BenchmarkPlan",
    "Classification", "Component", "Dependency", "NotApproved", "PlanError",
    "PlanNotFound", "PlanStatus", "PlanStore", "PlanTask", "ProjectArchitect",
    "ProjectKind", "ProjectPlan", "ReleasePlan", "Requirement",
    "Specification", "TestPlan", "acceptance_criteria_for", "classify",
    "constraints_from", "goals_from",
]
