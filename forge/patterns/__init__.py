"""Pattern discovery engine (A84 Stage H).

Public surface: :class:`forge.patterns.graph.PatternGraph` — a typed,
evidence-backed relationship layer over Forge's conversations, research,
code, projects, agents and user preferences, with explicit contradiction
adjudication (nothing is silently overwritten).
"""
from forge.patterns.graph import (
    ENTITY_TYPES,
    RELATION_STATUSES,
    Entity,
    PatternGraph,
    Relation,
)

__all__ = ["Entity", "PatternGraph", "Relation", "ENTITY_TYPES",
           "RELATION_STATUSES"]
